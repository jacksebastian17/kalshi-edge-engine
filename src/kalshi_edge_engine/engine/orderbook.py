"""Orderbook parsing and analysis utilities.

Kalshi's orderbook provides BID ladders for YES and NO sides.
There are no explicit ASK prices; instead, we compute cross prices.

Key concepts:
- Best bid = highest price in YES array, highest price in NO array
- Cross price (implied ask) = 100 - opposite_side_best_bid
  - buy_yes_cross = 100 - best_no_bid (price to immediately complete YES trade)
  - buy_no_cross = 100 - best_yes_bid
- Inside width = cross - best_bid (room to quote between bid and cross)
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from kalshi_edge_engine.engine.fees import LEGACY_FEE_PROFILE, FeeProfile


class BestLevel(NamedTuple):
    """Best level information (bid or cross)."""

    price: float | None  # Price in cents (0-100)
    size: int  # Size in contracts


class ParsedOrderbook(NamedTuple):
    """Parsed orderbook with best bids and cross prices for each side."""

    bid_yes: BestLevel | None  # Best YES bid (highest in YES ladder)
    cross_yes: BestLevel | None  # Cross price for YES (100 - best_no_bid)
    bid_no: BestLevel | None  # Best NO bid (highest in NO ladder)
    cross_no: BestLevel | None  # Cross price for NO (100 - best_yes_bid)


@dataclass(frozen=True)
class LiquidationQuote:
    """Whole-position paper sale at displayed bids, with fees rounded per level."""

    size_contracts: int
    price_cents: float
    exit_fee_cents: float
    best_bid_cents: float
    implied_ask_cents: float | None
    bid_levels: tuple[tuple[float, int], ...]
    observed_ts_ms: int
    fee_profile: FeeProfile = LEGACY_FEE_PROFILE

    @property
    def midpoint_cents(self) -> float | None:
        if self.implied_ask_cents is None:
            return None
        return (self.best_bid_cents + self.implied_ask_cents) / 2

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        return 0 <= now_ms - self.observed_ts_ms <= max_age_ms


def quote_liquidation(
    orderbook: dict[str, Any],
    side: str,
    size_contracts: int,
    observed_ts_ms: int,
    *,
    fee_profile: FeeProfile = LEGACY_FEE_PROFILE,
) -> LiquidationQuote | None:
    """Price an immediate full sale against the held contract's bid ladder."""
    if not isinstance(orderbook, dict) or side not in ("YES", "NO") or size_contracts <= 0:
        return None
    yes, no, dollars = _extract_prices_from(orderbook)
    bids = _liquidation_levels(yes if side == "YES" else no, dollars)
    opposite_bids = _liquidation_levels(no if side == "YES" else yes, dollars)
    if not bids:
        return None
    implied_ask = 100 - opposite_bids[0][0] if opposite_bids else None
    if implied_ask is not None and bids[0][0] > implied_ask + 1e-9:
        return None

    remaining = size_contracts
    levels: list[tuple[float, int]] = []
    for price, available in bids:
        size = min(available, remaining)
        levels.append((price, size))
        remaining -= size
        if remaining == 0:
            return LiquidationQuote(
                size_contracts=size_contracts,
                price_cents=sum(price * size for price, size in levels) / size_contracts,
                exit_fee_cents=sum(
                    fee_profile.fee_cents(price, size, liquidity="taker", sale=True)
                    for price, size in levels
                ),
                best_bid_cents=bids[0][0],
                implied_ask_cents=implied_ask,
                bid_levels=tuple(levels),
                observed_ts_ms=observed_ts_ms,
                fee_profile=fee_profile,
            )
    return None


def _liquidation_levels(prices: list[Any], dollars: bool) -> list[tuple[float, int]]:
    levels: dict[float, int] = {}
    for item in prices if isinstance(prices, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        price = _parse_price_cents(item[0], prices_are_dollars=dollars)
        # Paper inventory is integer-sized. Count only whole contracts per level.
        try:
            count = Decimal(str(item[1]))
            size = int(count) if count.is_finite() and count > 0 else 0
        except InvalidOperation:
            continue
        if price is not None and 0 < price < 100 and size > 0:
            # A repeated level in a snapshot is ambiguous. Count it only once.
            levels[price] = min(levels.get(price, size), size)
    return sorted(levels.items(), reverse=True)


def _extract_prices_from(data: dict[str, Any]) -> tuple[list[Any], list[Any], bool]:
    """Extract YES and NO price ladders from orderbook data.

    Handles both nested shape (orderbook["orderbook"]["yes"]) and flat (orderbook["yes"]).

    Args:
        data: Orderbook dict (either nested or flat).

    Returns:
        Tuple of (yes_prices, no_prices, prices_are_dollars).
    """
    # Fixed-point shape (Kalshi post-migration): orderbook_fp.yes_dollars/no_dollars
    if "orderbook_fp" in data and isinstance(data["orderbook_fp"], dict):
        yes_prices = data["orderbook_fp"].get("yes_dollars", [])
        no_prices = data["orderbook_fp"].get("no_dollars", [])
        return yes_prices, no_prices, True

    # Try nested shape first
    if "orderbook" in data and isinstance(data["orderbook"], dict):
        yes_prices = data["orderbook"].get("yes", [])
        no_prices = data["orderbook"].get("no", [])
        return yes_prices, no_prices, False

    # Fall back to flat shape
    yes_prices = data.get("yes", [])
    no_prices = data.get("no", [])
    return yes_prices, no_prices, False


def _parse_size_contracts(size_value: Any) -> int | None:
    """Parse external size into internal integer contract count.

    Supports fixed-point string counts like "16642.00" while preserving
    integer-contract internal semantics.
    """
    if isinstance(size_value, str):
        try:
            decimal_size = Decimal(size_value)
        except (InvalidOperation, ValueError):
            return None

        if decimal_size < 0:
            return None

        integral_size = decimal_size.to_integral_value()
        if decimal_size != integral_size:
            return None

        return int(integral_size)

    try:
        parsed_size = int(size_value)
    except (ValueError, TypeError):
        return None

    return parsed_size if parsed_size >= 0 else None


def _parse_price_cents(price_value: Any, *, prices_are_dollars: bool) -> float | None:
    """Parse external price into internal cent-based float representation."""
    try:
        if prices_are_dollars:
            price_dollars = Decimal(str(price_value))
            if price_dollars < 0 or price_dollars > 1:
                return None
            return float(price_dollars * Decimal("100"))

        price_cents = float(price_value)
        if 0.0 <= price_cents <= 100.0:
            return price_cents
    except (InvalidOperation, ValueError, TypeError):
        return None

    return None


def displayed_quote_size(book: dict[str, Any], side: str, quote: float) -> float | None:
    """Reported size at the real bid level, including levels below the best bid."""
    yes, no, dollars = _extract_prices_from(book)
    levels = yes if side == "YES" else no
    for level in levels if isinstance(levels, list) else []:
        if (
            isinstance(level, (list, tuple))
            and len(level) >= 2
            and _parse_price_cents(level[0], prices_are_dollars=dollars) == quote
        ):
            try:
                count = Decimal(str(level[1]))
            except InvalidOperation:
                continue
            if count.is_finite() and count >= 0:
                return float(count)
    return None


def _extract_best_bid(prices: list[Any], *, prices_are_dollars: bool = False) -> BestLevel | None:
    """Extract best bid (highest price) from ladder.

    Validates entries: price must be 0-100, size must be >= 0.
    Skips invalid entries.

    Args:
        prices: List of [price, size] entries.

    Returns:
        BestLevel with highest price, or None if empty/invalid.
    """
    if not prices or not isinstance(prices, list):
        return None

    best_price: float | None = None
    best_size: int | None = None

    for item in prices:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue

        parsed_price = _parse_price_cents(item[0], prices_are_dollars=prices_are_dollars)
        parsed_size = _parse_size_contracts(item[1])

        if parsed_price is None or parsed_size is None:
            continue

        if best_price is None or parsed_price > best_price:
            best_price = parsed_price
            best_size = parsed_size

    if best_price is not None and best_size is not None:
        return BestLevel(price=best_price, size=best_size)

    return None


def parse_orderbook(orderbook: dict[str, Any]) -> ParsedOrderbook:
    """Parse Kalshi orderbook response to extract best bids and cross prices.

    Kalshi provides BID ladders for YES and NO sides:
    - "yes": [[price_cents, size], ...] — prices people willing to BUY YES
    - "no":  [[price_cents, size], ...] — prices people willing to BUY NO

    Best bid on each side = highest price in that ladder.

    Cross prices (what you'd pay to immediately buy):
    - cross_yes = "best immediate YES buy price implied by NO bids"
    - cross_no = "best immediate NO buy price implied by YES bids"

    Cross prices have size=0 (they do not represent actual liquidity).

    Args:
        orderbook: Raw orderbook dict from Kalshi API.
                   Supports both nested shape (orderbook["orderbook"]["yes"])
                   and flat shape (orderbook["yes"]).

    Returns:
        ParsedOrderbook with best bids and cross prices.
        Returns None for prices if data is missing or malformed.
    """
    bid_yes: BestLevel | None = None
    cross_yes: BestLevel | None = None
    bid_no: BestLevel | None = None
    cross_no: BestLevel | None = None

    try:
        # Extract YES and NO ladders (handles fixed-point, nested, and flat shapes)
        yes_prices, no_prices, prices_are_dollars = _extract_prices_from(orderbook)

        # Extract best bids
        bid_yes = _extract_best_bid(yes_prices, prices_are_dollars=prices_are_dollars)
        bid_no = _extract_best_bid(no_prices, prices_are_dollars=prices_are_dollars)

        # Compute cross prices from opposite-side best bids (cross prices have size=0)
        if bid_no and bid_no.price is not None:
            cross_yes_price = 100.0 - bid_no.price
            cross_yes = BestLevel(price=cross_yes_price, size=0)

        if bid_yes and bid_yes.price is not None:
            cross_no_price = 100.0 - bid_yes.price
            cross_no = BestLevel(price=cross_no_price, size=0)

    except (ValueError, TypeError, AttributeError, KeyError):
        # If any parsing fails, return what we have (prefer partial data over crash)
        pass

    return ParsedOrderbook(bid_yes=bid_yes, cross_yes=cross_yes, bid_no=bid_no, cross_no=cross_no)


def get_inside_width(bid: float | None, cross: float | None) -> float | None:
    """Calculate inside width (cross - bid) for market-making opportunity.

    Inside width is the room available to quote between the best bid
    and the cross price. If inside_width > fees, there's room to provide
    liquidity and capture the spread.

    Args:
        bid: Best bid price (cents).
        cross: Cross price (cents).

    Returns:
        Inside width in cents, or None if either price is unavailable.
        Returns None if cross <= bid (no room or inverted).
    """
    if bid is None or cross is None:
        return None
    width = cross - bid
    return width if width > 0 else None


def get_best_bid_size(level: BestLevel | None) -> int:
    """Get liquidity at best bid level.

    Args:
        level: Best bid level.

    Returns:
        Size in contracts, or 0 if unavailable.
    """
    return level.size if level is not None else 0
