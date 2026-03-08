"""Runtime configuration for scanner and paper trading."""

import os

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in ("true", "1", "yes")


ENABLE_UNIVERSE_NBA = _env_bool("ENABLE_UNIVERSE_NBA", True)
ENABLE_UNIVERSE_CBB = _env_bool("ENABLE_UNIVERSE_CBB", False)

ENABLE_PAPER_TRADING = os.getenv("ENABLE_PAPER_TRADING", "false").lower() in (
    "true",
    "1",
    "yes",
)
PAPER_MIN_EDGE_CENTS = float(os.getenv("PAPER_MIN_EDGE_CENTS", "1.0"))
PAPER_ALLOW_YES_ENTRIES = os.getenv("PAPER_ALLOW_YES_ENTRIES", "true").lower() in (
    "true",
    "1",
    "yes",
)
PAPER_ALLOW_NO_ENTRIES = os.getenv("PAPER_ALLOW_NO_ENTRIES", "true").lower() in (
    "true",
    "1",
    "yes",
)
PAPER_MIN_TOP_LIQUIDITY = int(os.getenv("PAPER_MIN_TOP_LIQUIDITY", "0"))
PAPER_REQUIRE_SIGNAL_VALID_ONLY = os.getenv("PAPER_REQUIRE_SIGNAL_VALID_ONLY", "true").lower() in (
    "true",
    "1",
    "yes",
)
PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS = float(
    os.getenv("PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", "0")
)
PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS = float(os.getenv("PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", "0"))
PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE = int(
    os.getenv("PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", "0")
)
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


def get_enabled_universes() -> list[str]:
    """Return enabled universes in deterministic normalized order."""
    enabled: list[str] = []
    if ENABLE_UNIVERSE_NBA:
        enabled.append("NBA")
    if ENABLE_UNIVERSE_CBB:
        enabled.append("CBB")
    return enabled
