"""Runtime configuration for scanner and paper trading."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

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
# Intentionally global risk control: ticker-level concentration cap is shared
# across universes to keep exposure accounting conservative at portfolio scope.
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
# Path-aware entry controls (optional). Defaults preserve current behavior.
PAPER_ALLOW_PRICE_IMPROVES = os.getenv("PAPER_ALLOW_PRICE_IMPROVES", "true").lower() in (
    "true",
    "1",
    "yes",
)
PAPER_ALLOW_MOVED_THROUGH = os.getenv("PAPER_ALLOW_MOVED_THROUGH", "true").lower() in (
    "true",
    "1",
    "yes",
)
_paper_min_edge_price_improves_raw = os.getenv("PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES")
PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES = (
    float(_paper_min_edge_price_improves_raw.strip())
    if _paper_min_edge_price_improves_raw is not None
    and _paper_min_edge_price_improves_raw.strip() != ""
    else None
)
_paper_min_edge_moved_through_raw = os.getenv("PAPER_MIN_EDGE_CENTS_MOVED_THROUGH")
PAPER_MIN_EDGE_CENTS_MOVED_THROUGH = (
    float(_paper_min_edge_moved_through_raw.strip())
    if _paper_min_edge_moved_through_raw is not None
    and _paper_min_edge_moved_through_raw.strip() != ""
    else None
)
_paper_max_entry_samples_per_ticker_per_hour_raw = os.getenv(
    "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR"
)
PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR = (
    int(_paper_max_entry_samples_per_ticker_per_hour_raw.strip())
    if _paper_max_entry_samples_per_ticker_per_hour_raw is not None
    and _paper_max_entry_samples_per_ticker_per_hour_raw.strip() != ""
    else None
)
_paper_max_entry_samples_per_ticker_side_per_hour_raw = os.getenv(
    "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR"
)
PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR = (
    int(_paper_max_entry_samples_per_ticker_side_per_hour_raw.strip())
    if _paper_max_entry_samples_per_ticker_side_per_hour_raw is not None
    and _paper_max_entry_samples_per_ticker_side_per_hour_raw.strip() != ""
    else None
)
_paper_min_distinct_tickers_for_strategy_confidence_raw = os.getenv(
    "PAPER_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE"
)
PAPER_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE = (
    int(_paper_min_distinct_tickers_for_strategy_confidence_raw.strip())
    if _paper_min_distinct_tickers_for_strategy_confidence_raw is not None
    and _paper_min_distinct_tickers_for_strategy_confidence_raw.strip() != ""
    else None
)
PAPER_SIMULATED_VOLUME_PER_POLL = int(os.getenv("PAPER_SIMULATED_VOLUME_PER_POLL", "50"))
PAPER_MAX_INTENTS_PER_MINUTE = int(os.getenv("PAPER_MAX_INTENTS_PER_MINUTE", "10"))
PAPER_TICKER_COOLDOWN_SECONDS = int(os.getenv("PAPER_TICKER_COOLDOWN_SECONDS", "120"))
PAPER_MAX_DAILY_LOSS_CENTS = float(os.getenv("PAPER_MAX_DAILY_LOSS_CENTS", "0"))  # 0 = disabled
PAPER_PNL_SNAPSHOT_INTERVAL_MS = 30_000

T = TypeVar("T")


@dataclass(frozen=True)
class PaperRuntimeConfig:
    """Resolved paper configuration for a specific universe."""

    universe: str
    min_edge_cents: float
    allow_yes_entries: bool
    allow_no_entries: bool
    min_top_liquidity: int
    require_signal_valid_only: bool
    min_capturable_edge_to_cross_cents: float
    min_abs_distance_from_50_cents: float
    min_reentry_delay_seconds_same_ticker_side: int
    take_profit_cents_per_contract: float
    max_hold_seconds: int
    fill_horizon_seconds: int
    poll_interval_seconds: float
    fill_probability_price_moved: float
    fill_probability_at_best: float
    fill_probability_price_improves: float
    allow_price_improves: bool
    allow_moved_through: bool
    min_edge_cents_price_improves: float | None
    min_edge_cents_moved_through: float | None
    max_entry_samples_per_ticker_per_hour: int | None
    max_entry_samples_per_ticker_side_per_hour: int | None
    min_distinct_tickers_for_strategy_confidence: int | None
    simulated_volume_per_poll: int
    ticker_cooldown_seconds: int


def _env_override(
    *,
    universe: str,
    suffix: str,
    parser: Callable[[str], T],
) -> T | None:
    """Parse optional universe-specific override from env.

    Environment variable shape: PAPER_<UNIVERSE>_<SUFFIX>
    Example: PAPER_NBA_MIN_EDGE_CENTS
    """
    key = f"PAPER_{universe.upper().strip()}_{suffix}"
    raw = os.getenv(key)
    if raw is None:
        return None
    return parser(raw)


def _parse_env_bool(raw: str) -> bool:
    return raw.lower() in ("true", "1", "yes")


def _parse_env_optional_float(raw: str) -> float | None:
    stripped = raw.strip()
    if stripped == "":
        return None
    return float(stripped)


def _parse_env_optional_int(raw: str) -> int | None:
    stripped = raw.strip()
    if stripped == "":
        return None
    return int(stripped)


def _resolve_with_override(
    *,
    universe: str,
    suffix: str,
    default: T,
    parser: Callable[[str], T],
) -> T:
    override = _env_override(universe=universe, suffix=suffix, parser=parser)
    return default if override is None else override


def get_effective_paper_config(universe: str) -> PaperRuntimeConfig:
    """Resolve effective paper config for a universe.

    Falls back to existing global PAPER_* values when no universe override exists.
    """
    universe_key = (universe or "NBA").upper().strip()
    return PaperRuntimeConfig(
        universe=universe_key,
        min_edge_cents=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_EDGE_CENTS",
            default=PAPER_MIN_EDGE_CENTS,
            parser=float,
        ),
        allow_yes_entries=_resolve_with_override(
            universe=universe_key,
            suffix="ALLOW_YES_ENTRIES",
            default=PAPER_ALLOW_YES_ENTRIES,
            parser=_parse_env_bool,
        ),
        allow_no_entries=_resolve_with_override(
            universe=universe_key,
            suffix="ALLOW_NO_ENTRIES",
            default=PAPER_ALLOW_NO_ENTRIES,
            parser=_parse_env_bool,
        ),
        min_top_liquidity=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_TOP_LIQUIDITY",
            default=PAPER_MIN_TOP_LIQUIDITY,
            parser=int,
        ),
        require_signal_valid_only=_resolve_with_override(
            universe=universe_key,
            suffix="REQUIRE_SIGNAL_VALID_ONLY",
            default=PAPER_REQUIRE_SIGNAL_VALID_ONLY,
            parser=_parse_env_bool,
        ),
        min_capturable_edge_to_cross_cents=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS",
            default=PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS,
            parser=float,
        ),
        min_abs_distance_from_50_cents=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_ABS_DISTANCE_FROM_50_CENTS",
            default=PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS,
            parser=float,
        ),
        min_reentry_delay_seconds_same_ticker_side=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE",
            default=PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE,
            parser=int,
        ),
        take_profit_cents_per_contract=_resolve_with_override(
            universe=universe_key,
            suffix="TAKE_PROFIT_CENTS_PER_CONTRACT",
            default=PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT,
            parser=float,
        ),
        max_hold_seconds=_resolve_with_override(
            universe=universe_key,
            suffix="MAX_HOLD_SECONDS",
            default=PAPER_MAX_HOLD_SECONDS,
            parser=int,
        ),
        fill_horizon_seconds=_resolve_with_override(
            universe=universe_key,
            suffix="FILL_HORIZON_SECONDS",
            default=PAPER_FILL_HORIZON_SECONDS,
            parser=int,
        ),
        poll_interval_seconds=_resolve_with_override(
            universe=universe_key,
            suffix="POLL_INTERVAL_SECONDS",
            default=PAPER_POLL_INTERVAL_SECONDS,
            parser=float,
        ),
        fill_probability_price_moved=_resolve_with_override(
            universe=universe_key,
            suffix="FILL_PROBABILITY",
            default=PAPER_FILL_PROBABILITY,
            parser=float,
        ),
        fill_probability_at_best=_resolve_with_override(
            universe=universe_key,
            suffix="FILL_PROBABILITY_AT_BEST",
            default=PAPER_FILL_PROBABILITY_AT_BEST,
            parser=float,
        ),
        fill_probability_price_improves=_resolve_with_override(
            universe=universe_key,
            suffix="FILL_PROBABILITY_PRICE_IMPROVES",
            default=PAPER_FILL_PROBABILITY_PRICE_IMPROVES,
            parser=float,
        ),
        allow_price_improves=_resolve_with_override(
            universe=universe_key,
            suffix="ALLOW_PRICE_IMPROVES",
            default=PAPER_ALLOW_PRICE_IMPROVES,
            parser=_parse_env_bool,
        ),
        allow_moved_through=_resolve_with_override(
            universe=universe_key,
            suffix="ALLOW_MOVED_THROUGH",
            default=PAPER_ALLOW_MOVED_THROUGH,
            parser=_parse_env_bool,
        ),
        min_edge_cents_price_improves=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_EDGE_CENTS_PRICE_IMPROVES",
            default=PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES,
            parser=_parse_env_optional_float,
        ),
        min_edge_cents_moved_through=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_EDGE_CENTS_MOVED_THROUGH",
            default=PAPER_MIN_EDGE_CENTS_MOVED_THROUGH,
            parser=_parse_env_optional_float,
        ),
        max_entry_samples_per_ticker_per_hour=_resolve_with_override(
            universe=universe_key,
            suffix="MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR",
            default=PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR,
            parser=_parse_env_optional_int,
        ),
        max_entry_samples_per_ticker_side_per_hour=_resolve_with_override(
            universe=universe_key,
            suffix="MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR",
            default=PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR,
            parser=_parse_env_optional_int,
        ),
        min_distinct_tickers_for_strategy_confidence=_resolve_with_override(
            universe=universe_key,
            suffix="MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE",
            default=PAPER_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE,
            parser=_parse_env_optional_int,
        ),
        simulated_volume_per_poll=_resolve_with_override(
            universe=universe_key,
            suffix="SIMULATED_VOLUME_PER_POLL",
            default=PAPER_SIMULATED_VOLUME_PER_POLL,
            parser=int,
        ),
        ticker_cooldown_seconds=_resolve_with_override(
            universe=universe_key,
            suffix="TICKER_COOLDOWN_SECONDS",
            default=PAPER_TICKER_COOLDOWN_SECONDS,
            parser=int,
        ),
    )


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
