"""Stage 2C measurements using the base commit's orderbook, fee model and scorer."""

import hashlib
from decimal import Decimal, DecimalException, InvalidOperation
from inspect import signature
from pathlib import Path

from kalshi_edge_engine.engine import fees, orderbook, signal
from kalshi_edge_engine.kalshi.market import timestamp_ms

BASE = "b33dc6d78db48240cfc107ba8eac3201be09ead6"
PINS = {
    "engine/signal.py": "b388674d0ffb1eb6d2a6f2c2d638dd3abcf60245d44ab1087a856603de47537b",
    "engine/orderbook.py": "b881e5236da91e72f71af1b36faf9b76a18617f32b15e72487d1849214a44d55",
    "engine/fees.py": "ee51f41be31332b4230c5bdd059e55770bf865841bc49faf71e583c3f2e25c34",
    "kalshi/market.py": "31232dc775b00b69c01bf88090adae58ad50dd4c7c2c7882bf5b5e05c49e92cf",
    "runtime/scanner_runtime.py": "1b150e6bf78530a23c56aa09e54ff40775714391f28f5e1dd8934315c991be11",
    "runtime/market_fees.py": "874915a2a477011775321bd4ce20234410644e71749724ef06c7110671e26830",
}


def source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def verify_scorer() -> None:
    root = Path(signal.__file__).resolve().parents[1]
    if any(source_hash(root / name) != pin for name, pin in PINS.items()):
        raise ValueError(f"Stage 2C requires the frozen signal source at {BASE}")


def branch(s: signal.Signal) -> str:
    # Disambiguate only the return paths of the hash-pinned scorer, in its precedence order.
    if s.reason_code == signal.MARKET_INELIGIBLE:
        return "missing_best_bid" if s.bid_sum is None else "crossed_bid_sum"
    if s.reason_code == signal.SPREAD_TOO_SMALL:
        threshold = signature(signal.score_signal).parameters["imbalance_threshold"].default
        if s.imbalance_gap is not None and s.imbalance_gap < threshold:
            return "structural_gap_below_threshold"
        if s.intended_quote_yes_cents is None and s.intended_quote_no_cents is None:
            return "neither_inside_quote_constructable"
        return "no_eligible_side_meets_net_edge"
    return {
        signal.FEE_DATA_UNAVAILABLE: "fee_profile_unavailable",
        signal.LOW_LIQUIDITY: "usable_quotes_below_side_liquidity",
        signal.SIGNAL_VALID: "eligible_side_meets_net_edge",
    }[s.reason_code]


def tick_cents(ranges: object, bid: Decimal | None) -> str | None:
    if bid is None or not isinstance(ranges, list):
        return None
    steps = set()
    for row in ranges:
        try:
            start, end, step = (Decimal(str(row[k])) * 100 for k in ("start", "end", "step"))
            if (
                not all(x.is_finite() for x in (start, end, step))
                or not 0 <= start < end <= 100
                or step <= 0
            ):
                return None
            # At a shared boundary return a tick only when both published steps agree.
            if start <= bid <= end:
                if (bid - start) % step:
                    return None
                steps.add(step)
        except (KeyError, TypeError, InvalidOperation, ValueError):
            return None
    return str(steps.pop()) if len(steps) == 1 else None


def measurements(
    ticker: str,
    raw_book: dict,
    event: dict,
    series: dict,
    at_ms: int,
    *,
    fee_observed_ms: int | None = None,
) -> dict:
    game = ticker.rpartition("-")[0]
    if event.get("event_ticker") != game or event.get("series_ticker") != "KXNBAGAME":
        event = {}
    markets = event.get("markets")
    matches = (
        [
            m
            for m in markets
            if isinstance(m, dict) and m.get("ticker") == ticker and m.get("event_ticker") == game
        ]
        if isinstance(markets, list)
        else []
    )
    m = matches[0] if len(matches) == 1 else {}
    profile = None
    close = timestamp_ms(m.get("close_time"))
    waiver = m.get("fee_waiver_expiration_time")
    waiver_end = timestamp_ms(waiver)
    if (
        m.get("status") == "active"
        and (close is None or at_ms < close)
        and (not waiver or (waiver_end is not None and waiver_end <= at_ms))
        and series.get("ticker") == "KXNBAGAME"
    ):
        profile = fees.series_fee_profile(
            series, event, observed_ts_ms=at_ms if fee_observed_ms is None else fee_observed_ms
        )
    b = orderbook.canonical_book(raw_book)
    score_error = None
    try:
        s = signal.score_signal(ticker, raw_book, fee_profile=profile)
    except (DecimalException, OverflowError) as exc:
        # Keep the book if a venue numeric input exceeds the frozen model's precision.
        s = None
        score_error = type(exc).__name__
    sides = {}
    for side in ("YES", "NO"):
        top = b.top(side)
        opposite = b.top("NO" if side == "YES" else "YES")
        ask = b.ask(side)
        sides[side] = {
            "bid_cents": str(top.price) if top else None,
            "implied_ask_cents": str(ask) if ask is not None else None,
            "same_side_top_contracts": str(top.quantity) if top else None,
            "opposite_side_top_contracts": str(opposite.quantity) if opposite else None,
            "bid_tick_cents": tick_cents(m.get("price_ranges"), top.price if top else None),
        }
    yes, no = b.top("YES"), b.top("NO")
    return {
        "sides": sides,
        "spread_cents": str(100 - yes.price - no.price) if yes and no else None,
        "sum_top_contracts": str(yes.quantity + no.quantity) if yes and no else None,
        "max_top_contracts": str(max(yes.quantity, no.quantity)) if yes and no else None,
        "scorer_top_whole_contracts": s.top_liquidity if s else None,
        "price_level_structure": m.get("price_level_structure"),
        "price_ranges_dollars": m.get("price_ranges"),
        "market_status": m.get("status"),
        "fee_profile": profile.audit() if profile else None,
        "signal_reason": s.reason_code if s else None,
        "signal_branch": branch(s) if s else None,
        "score_error": score_error,
        "book_issues": list(b.issues),
    }
