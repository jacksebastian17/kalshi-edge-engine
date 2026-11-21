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

from collections.abc import Iterator
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


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal  # cents
    quantity: Decimal  # reported contracts

    @property
    def capacity(self) -> int:
        return int(self.quantity)

    def json_quantity(self) -> int | float | str:
        if self.quantity == self.capacity:
            return self.capacity
        number = float(self.quantity)
        return number if Decimal(str(number)) == self.quantity else str(self.quantity)


@dataclass(frozen=True, slots=True)
class Book:
    yes: tuple[Level, ...]
    no: tuple[Level, ...]
    issues: tuple[str, ...]

    def levels(self, side: str) -> tuple[Level, ...]:
        if side not in ("YES", "NO"):
            raise ValueError("Unknown contract side")
        return self.yes if side == "YES" else self.no

    def top(self, side: str) -> Level | None:
        levels = self.levels(side)
        return levels[0] if levels else None

    def at(self, side: str, price: Decimal) -> Level | None:
        return next((level for level in self.levels(side) if level.price == price), None)

    def ask(self, side: str) -> Decimal | None:
        opposite = self.top("NO" if side == "YES" else "YES")
        return 100 - opposite.price if opposite else None

    def whole_bids(self, side: str) -> Iterator[tuple[Decimal, int]]:
        """Executable capacity per price, ordered from best to worst."""
        return ((level.price, level.capacity) for level in self.levels(side) if level.capacity)


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, str) and (not value.isascii() or value != value.strip() or "_" in value):
        return None
    try:
        n = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    # Exact arithmetic and bounded conversion to SQLite-compatible integer sizes.
    if not n.is_finite() or n < 0 or n > 2**63 - 1:
        return None
    if n != n.quantize(Decimal("0.00000001")):
        return None
    return n


def canonical_book(data: dict[str, Any] | Book) -> Book:
    if isinstance(data, Book):
        return data
    if not isinstance(data, dict):
        return Book((), (), ("invalid_book",))
    fixed = "orderbook_fp" in data
    ladder = data.get("orderbook_fp") if fixed else data.get("orderbook", data)
    if not isinstance(ladder, dict):
        return Book((), (), ("invalid_book",))
    issues: set[str] = set()

    def levels(key: str) -> tuple[Level, ...]:
        rows = ladder.get(key)
        if not isinstance(rows, list):
            issues.add(f"invalid_array_{key}" if key in ladder else f"missing_{key}")
            return ()
        by_price: dict[Decimal, Decimal] = {}
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                issues.add("invalid_level")
                continue
            price, size = _number(row[0]), _number(row[1])
            if price is None or size is None:
                issues.add("invalid_number")
                continue
            cents = price * 100 if fixed else price
            if not 0 < cents < 100 or cents != cents.quantize(Decimal("0.01")):
                issues.add("invalid_price")
                continue
            if cents in by_price:
                issues.add("duplicate_price")
                size = min(by_price[cents], size)
            by_price[cents] = size
        return tuple(
            Level(p, by_price[p]) for p in sorted(by_price, reverse=True) if by_price[p] > 0
        )

    yes = levels("yes_dollars" if fixed else "yes")
    no = levels("no_dollars" if fixed else "no")
    return Book(yes, no, tuple(sorted(issues)))


def parse_orderbook(orderbook: dict[str, Any] | Book) -> ParsedOrderbook:
    """Compatibility view for signal and telemetry. Bids expose whole-contract capacity."""
    book = canonical_book(orderbook)

    def bid(side: str) -> BestLevel | None:
        level = book.top(side)
        return BestLevel(float(level.price), level.capacity) if level else None

    def cross(side: str) -> BestLevel | None:
        price = book.ask(side)
        return BestLevel(float(price), 0) if price is not None else None

    return ParsedOrderbook(bid("YES"), cross("YES"), bid("NO"), cross("NO"))


def displayed_quote_size(
    book: dict[str, Any] | Book, side: str, quote: float
) -> int | float | str | None:
    """JSON-compatible reported size. Keep an exact string when a JSON float would lose digits."""
    level = canonical_book(book).at(side, Decimal(str(quote)))
    if level is None:
        return None
    return level.json_quantity()


def quote_liquidation(
    orderbook: dict[str, Any] | Book,
    side: str,
    size_contracts: int,
    observed_ts_ms: int,
    *,
    fee_profile: FeeProfile = LEGACY_FEE_PROFILE,
) -> LiquidationQuote | None:
    """Sweep whole capacity per distinct level while retaining the displayed top."""
    if side not in ("YES", "NO") or type(size_contracts) is not int or size_contracts <= 0:
        return None
    book = canonical_book(orderbook)
    top, ask = book.top(side), book.ask(side)
    if top is None or (ask is not None and top.price > ask):
        return None
    remaining = size_contracts
    taken: list[tuple[float, int]] = []
    total = Decimal(0)
    for price, available in book.whole_bids(side):
        count = min(available, remaining)
        total += price * count
        taken.append((float(price), count))
        remaining -= count
        if remaining == 0:
            return LiquidationQuote(
                size_contracts,
                float(total / size_contracts),
                sum(fee_profile.fee_cents(p, n, liquidity="taker", sale=True) for p, n in taken),
                float(top.price),
                float(ask) if ask is not None else None,
                tuple(taken),
                observed_ts_ms,
                fee_profile,
            )
    return None


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
