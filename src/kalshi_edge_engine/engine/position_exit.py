"""Position exit evaluation helpers for paper trading lifecycle.

This module is intentionally side-effect free so that position lifecycle
decisions are deterministic and easy to unit test.
"""

from dataclasses import dataclass


def compute_price_delta_cents_per_contract(
    *,
    side: str,
    avg_entry_price_cents: float,
    mark_price_cents: float,
) -> float:
    """Compute the price change for a long contract in its own price coordinates.

    Positive values indicate profitable movement.
    """
    if side.upper() not in ("YES", "NO"):
        raise ValueError(f"Invalid contract side: {side}")
    return mark_price_cents - avg_entry_price_cents


def should_take_profit(
    *,
    side: str,
    avg_entry_price_cents: float,
    mark_price_cents: float,
    take_profit_cents_per_contract: float,
) -> bool:
    """Return True when per-contract take-profit threshold is met.

    Semantics: `take_profit_cents_per_contract` is a per-contract threshold,
    not a total-position PnL threshold.
    """
    if take_profit_cents_per_contract <= 0:
        return False
    price_delta = compute_price_delta_cents_per_contract(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents,
    )
    return price_delta >= take_profit_cents_per_contract


def should_max_hold_exit(*, first_open_ts_ms: int, now_ms: int, max_hold_seconds: int) -> bool:
    """Return True if position has exceeded max hold duration."""
    if max_hold_seconds <= 0:
        return False
    hold_elapsed_ms = now_ms - first_open_ts_ms
    return hold_elapsed_ms >= (max_hold_seconds * 1000)


def compute_realized_exit_pnl_cents(
    *,
    side: str,
    avg_entry_price_cents: float,
    mark_price_cents: float,
    net_contracts: int,
    exit_fee_cents: float,
) -> float:
    """Compute fee-aware realized PnL increment for closing a position."""
    price_delta = compute_price_delta_cents_per_contract(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents,
    )
    gross_pnl_cents = price_delta * net_contracts
    return gross_pnl_cents - exit_fee_cents


@dataclass(frozen=True)
class PositionExitDecision:
    """Decision object for current mark-based position close checks."""

    should_close: bool
    reason: str | None


def decide_position_exit(
    *,
    side: str,
    avg_entry_price_cents: float,
    mark_price_cents: float,
    first_open_ts_ms: int,
    now_ms: int,
    max_hold_seconds: int,
    take_profit_cents_per_contract: float,
    liquidation_fees_cents_per_contract: float = 0.0,
) -> PositionExitDecision:
    """Decide if position should be closed at current mark.

    Decision precedence is conservative and deterministic:
    1) take_profit
    2) time_stop
    """
    if should_take_profit(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents - liquidation_fees_cents_per_contract,
        take_profit_cents_per_contract=take_profit_cents_per_contract,
    ):
        return PositionExitDecision(should_close=True, reason="take_profit")

    if should_max_hold_exit(
        first_open_ts_ms=first_open_ts_ms,
        now_ms=now_ms,
        max_hold_seconds=max_hold_seconds,
    ):
        return PositionExitDecision(should_close=True, reason="time_stop")

    return PositionExitDecision(should_close=False, reason=None)
