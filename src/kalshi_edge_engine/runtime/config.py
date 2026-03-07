"""Runtime configuration for scanner and paper trading."""

import os

from dotenv import load_dotenv

load_dotenv()

ENABLE_PAPER_TRADING = os.getenv("ENABLE_PAPER_TRADING", "false").lower() in (
    "true",
    "1",
    "yes",
)
PAPER_MIN_EDGE_CENTS = float(os.getenv("PAPER_MIN_EDGE_CENTS", "1.0"))
PAPER_MAX_CONCURRENT_INTENTS = int(os.getenv("PAPER_MAX_CONCURRENT_INTENTS", "5"))
PAPER_MAX_OPEN_ORDERS = int(os.getenv("PAPER_MAX_OPEN_ORDERS", "20"))
PAPER_MAX_POS_PER_TICKER = int(os.getenv("PAPER_MAX_POS_PER_TICKER", "3"))
PAPER_MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "20"))
PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT = float(
    os.getenv(
        "PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT",
        os.getenv("PAPER_TAKE_PROFIT_CENTS", "2.0"),
    )
)
# Backward-compatible alias (semantics are cents PER CONTRACT).
PAPER_TAKE_PROFIT_CENTS = PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
PAPER_MAX_HOLD_SECONDS = int(os.getenv("PAPER_MAX_HOLD_SECONDS", "600"))
PAPER_FILL_HORIZON_SECONDS = int(os.getenv("PAPER_FILL_HORIZON_SECONDS", "90"))
PAPER_POLL_INTERVAL_SECONDS = float(os.getenv("PAPER_POLL_INTERVAL_SECONDS", "3.0"))
PAPER_FILL_PROBABILITY = float(os.getenv("PAPER_FILL_PROBABILITY", "0.8"))
PAPER_FILL_PROBABILITY_AT_BEST = float(os.getenv("PAPER_FILL_PROBABILITY_AT_BEST", "0.15"))
PAPER_FILL_PROBABILITY_PRICE_IMPROVES = float(
    os.getenv("PAPER_FILL_PROBABILITY_PRICE_IMPROVES", "0.05")
)
PAPER_SIMULATED_VOLUME_PER_POLL = int(os.getenv("PAPER_SIMULATED_VOLUME_PER_POLL", "50"))
PAPER_MAX_INTENTS_PER_MINUTE = int(os.getenv("PAPER_MAX_INTENTS_PER_MINUTE", "10"))
PAPER_TICKER_COOLDOWN_SECONDS = int(os.getenv("PAPER_TICKER_COOLDOWN_SECONDS", "120"))
PAPER_MAX_DAILY_LOSS_CENTS = float(os.getenv("PAPER_MAX_DAILY_LOSS_CENTS", "0"))  # 0 = disabled
PAPER_PNL_SNAPSHOT_INTERVAL_MS = 30_000


def is_nba_scanner_enabled_from_env() -> bool:
    """Return whether NBA scanner autostart is enabled via environment."""
    return os.getenv("ENABLE_NBA_SCANNER", "false").lower() in ("true", "1", "yes")
