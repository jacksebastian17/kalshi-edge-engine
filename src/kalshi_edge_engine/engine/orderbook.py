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

from typing import Any, NamedTuple


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


def _extract_prices_from(data: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Extract YES and NO price ladders from orderbook data.

    Handles both nested shape (orderbook["orderbook"]["yes"]) and flat (orderbook["yes"]).

    Args:
        data: Orderbook dict (either nested or flat).

    Returns:
        Tuple of (yes_prices, no_prices) lists.
    """
    # Try nested shape first
    if "orderbook" in data and isinstance(data["orderbook"], dict):
        yes_prices = data["orderbook"].get("yes", [])
        no_prices = data["orderbook"].get("no", [])
        if yes_prices or no_prices:
            return yes_prices, no_prices

    # Fall back to flat shape
    yes_prices = data.get("yes", [])
    no_prices = data.get("no", [])
    return yes_prices, no_prices


def _extract_best_bid(prices: list[Any]) -> BestLevel | None:
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

    try:
        best_entry = max(
            (
                item
                for item in prices
                if isinstance(item, (list, tuple))
                and len(item) >= 2
                and 0.0 <= float(item[0]) <= 100.0
                and int(item[1]) >= 0
            ),
            key=lambda x: float(x[0]),
            default=None,
        )
        if best_entry:
            return BestLevel(price=float(best_entry[0]), size=int(best_entry[1]))
    except (ValueError, TypeError):
        pass

    return None


def parse_orderbook(orderbook: dict[str, Any]) -> ParsedOrderbook:
    """Parse Kalshi orderbook response to extract best bids and cross prices.

    Kalshi provides BID ladders for YES and NO sides:
    - "yes": [[price_cents, size], ...] — prices people willing to BUY YES
    - "no":  [[price_cents, size], ...] — prices people willing to BUY NO

    Best bid on each side = highest price in that ladder.

    Cross prices (what you'd pay to immediately sell):
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
        # Extract YES and NO ladders (handles nested and flat shapes)
        yes_prices, no_prices = _extract_prices_from(orderbook)

        # Extract best bids
        bid_yes = _extract_best_bid(yes_prices)
        bid_no = _extract_best_bid(no_prices)

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
