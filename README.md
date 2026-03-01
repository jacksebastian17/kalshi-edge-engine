# Kalshi Edge Engine

A Kalshi-only live signal engine designed to exploit exchange behavior — not predict outcomes.

This project builds a quant-style trading system using **only Kalshi API data**.
No external sportsbooks. No prediction models. No magic probability oracle.

Instead, we exploit:

* Spread inefficiencies
* Impatient flow
* Thin liquidity
* Market structure behavior

The goal is to build a real, measurable, auditable trading system that can run continuously and prove whether edge exists.

---

## Quick Start

### Prerequisites

- Python 3.11 or higher
- pip

### Installation

Clone the repository and install in development mode with all dependencies:

```bash
git clone https://github.com/yourusername/kalshi-edge-engine.git
cd kalshi-edge-engine
pip install -e ".[dev]"
```

### Running Locally

Start the API server:

```bash
uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

Or run it directly:

```bash
python -m src.api.main
```

The API will be available at `http://localhost:8000`.

**Health check:**
```bash
curl http://localhost:8000/health
```

**Version info:**
```bash
curl http://localhost:8000/version
```

### Running Tests

Run all tests:

```bash
pytest tests -v
```

With coverage:

```bash
pytest tests --cov=src --cov-report=html
```

### Linting & Type Checking

Format code with ruff:

```bash
ruff format src tests
```

Check linting:

```bash
ruff check src tests
```

Run type checker:

```bash
mypy src
```

---

## Project Structure

```
/src
  /kalshi              # Kalshi API client (stub)
  /engine              # Polling, data processing, signal generation
  /risk                # Eligibility filters, risk limits, kill switch
  /execution           # Order placement/management (paper mode only for now)
  /api                 # FastAPI application
  /db                  # Database models and migrations
  /utils               # Shared utilities
/tests                 # Pytest unit and integration tests
.github/workflows      # GitHub Actions CI/CD
```

---

## Core Philosophy

There are two ways to try to win on Kalshi:

1. Predict outcomes better than the market.
2. Exploit how the market behaves.

This project focuses on **#2**.

We are not trying to be sports analysts.
We are building an exchange behavior engine.

That means:

* We measure spreads.
* We measure liquidity.
* We measure volatility.
* We measure fill behavior.
* We measure realized PnL.
* We log everything.

If edge exists, we prove it with data.

---

## What This System Does

The system runs as a background daemon that:

1. Continuously pulls Kalshi orderbooks.
2. Computes real-time signals using only Kalshi prices.
3. Logs every market snapshot and decision.
4. Stores signals in a database.
5. Exposes a FastAPI backend for a live React dashboard.
6. Optionally places tiny live orders with strict guardrails.

It operates in three modes:

## Signals Mode

* No trading.
* Live “green signal” detection.
* Full logging.
* Used for research and validation.

## Paper Mode

* Simulated fills.
* Simulated PnL.
* Inventory tracking.
* Used to test strategy before risking real money.

## Auto Mode (Optional)

* Real orders.
* Very small size.
* Hard risk limits.
* Kill switch enabled.
* Disabled by default.

---

# Strategy v0: Spread Capture

The first strategy implemented is spread capture.

### Example

If:

* Best bid = 0.48
* Best ask = 0.51

Spread = 0.03

If the spread is large enough to exceed:

* Entry fee
* Exit fee
* Safety buffer

Then the system may place resting orders inside the spread.

Goal:

* Buy slightly cheaper
* Sell slightly higher
* Capture small edges repeatedly

No prediction required.

---

# What We Are Exploiting

We are exploiting:

1. **Impatience**
   Traders who cross the spread for immediate execution.

2. **Thin liquidity**
   Small books create temporary inefficiencies.

3. **Behavioral movement**
   Retail-heavy markets can move unevenly.

We are **not** exploiting:

* Insider information
* Fee rounding tricks
* External sportsbooks
* Illegal behavior

---

# Safety First

Auto-trading is dangerous.

This system enforces strict safeguards:

* Max order size
* Max total open exposure
* Max daily loss
* Max orders per minute
* Market eligibility filters
* Data freshness checks
* Hard kill switch

If daily loss exceeds threshold → auto mode shuts off.

Auto mode is **OFF by default**.

---

# Architecture Overview

```
Kalshi API
   ↓
Poller / Snapshot Engine
   ↓
Signal Engine
   ↓
Database (snapshots + signals + orders + fills)
   ↓
FastAPI Backend
   ↓
React Dashboard
```

## Core Components

### `/src/kalshi`

* API client
* Auth signing
* Orderbook types

### `/src/engine`

* Polling loop
* Signal generation
* Persistence

### `/src/risk`

* Eligibility rules
* Limits
* Kill switch

### `/src/execution`

* Order placement
* Cancel/replace logic
* Inventory management

### `/src/api`

* REST endpoints
* Health checks
* Control routes

### `/src/db`

* SQLAlchemy models
* Alembic migrations

### `/tests`

* Unit tests
* Strategy tests
* Risk tests

---

# Data Logging (Critical)

Every decision must be auditable.

For every signal, we log:

* Timestamp
* Market ticker
* Best bid
* Best ask
* Spread
* Fee estimate
* Net expected edge
* Liquidity at top
* Time to expiration
* Decision + reason code

If auto mode is enabled, we also log:

* Order submission
* Order cancellation
* Fills
* Inventory change
* Realized PnL
* Risk events

Nothing is hidden.
Everything is measurable.

---

# Configuration

Environment variables:

```
KALSHI_API_KEY_ID
KALSHI_PRIVATE_KEY_PATH
KALSHI_BASE_URL

TRADING_MODE=signals|paper|live
AUTO_TRADING=true|false

MAX_ORDER_SIZE
MAX_POSITION_SIZE
MAX_DAILY_LOSS
MIN_SPREAD_AFTER_FEES
MIN_TOP_LIQUIDITY
MIN_MINUTES_TO_EXPIRY
```

All configs are adjustable.

---

# Development Setup

## 1. Clone repo

```
git clone <repo-url>
cd kalshi-edge-engine
```

## 2. Create virtual environment

```
python -m venv .venv
source .venv/bin/activate
```

## 3. Install dependencies

```
pip install -e ".[dev]"
```

## 4. Configure environment

Copy `.env.example` to `.env` and fill in credentials.

## 5. Run database migrations

```
alembic upgrade head
```

## 6. Start daemon

```
python -m src.engine.daemon
```

## 7. Start API

```
uvicorn src.api.main:app --reload
```

## 8. Start React dashboard

```
cd web
npm install
npm run dev
```

---

# Metrics That Matter

This project measures:

* Fill rate
* Average captured spread
* Inventory duration
* Realized PnL
* Slippage
* Risk events triggered

We are not guessing.
We are measuring.

---

# Roadmap

## v0 — Signals Engine

* Kalshi client
* Orderbook polling
* Spread computation
* Signal detection
* Persistence
* Dashboard

## v1 — Paper Simulator

* Simulated fills
* Inventory tracking
* PnL computation
* Signal effectiveness analysis

## v2 — Auto Quoting

* Real order placement
* Cancel/replace engine
* Live PnL
* Hard guardrails

## v3 — Research Expansions

* Mean reversion
* Volatility modeling
* Market clustering
* Parameter optimization

---

# Why This Project Exists

This is a live quant lab.

The purpose is not just to trade.

The purpose is to:

* Understand exchange behavior.
* Measure edge honestly.
* Build real-time systems.
* Learn execution and risk control.
* Iterate with data.

If edge exists, we prove it.
If it doesn’t, we pivot intelligently.

---

# Disclaimer

Trading involves risk.
Auto mode should only be enabled with tiny size and strict safeguards.
This project is for research and educational purposes only.
