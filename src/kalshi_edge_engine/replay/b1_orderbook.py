"""Frozen B1 orderbook interpretation for offline audits of pre-B3A captures."""

from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_edge_engine.engine.fees import LEGACY_FEE_PROFILE, FeeProfile
from kalshi_edge_engine.engine.orderbook import BestLevel, LiquidationQuote, ParsedOrderbook


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


def _entry_side(prices: list[Any], dollars: bool) -> dict[float, int]:
    sizes: dict[float, int] = {}
    for row in prices if isinstance(prices, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        p = _parse_price_cents(row[0], prices_are_dollars=dollars)
        n = _parse_size_contracts(row[1])
        if p is not None and n is not None:
            sizes.setdefault(p, n)
    return sizes


def entry_depth(body: dict[str, Any]) -> dict[str, dict[Decimal, int]]:
    yes, no, dollars = _extract_prices_from(body)
    return {
        side: {Decimal(str(p)): n for p, n in _entry_side(rows, dollars).items()}
        for side, rows in (("YES", yes), ("NO", no))
    }


def _extract_best_bid(prices: list[Any], *, prices_are_dollars: bool = False) -> BestLevel | None:
    sizes = _entry_side(prices, prices_are_dollars)
    if not sizes:
        return None
    price = max(sizes)
    return BestLevel(price, sizes[price])


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
