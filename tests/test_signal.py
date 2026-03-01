"""Tests for signal scoring and orderbook parsing."""

import time
import unittest

from kalshi_edge_engine.engine.orderbook import (
    BestLevel,
    get_best_bid_size,
    get_inside_width,
    parse_orderbook,
)
from kalshi_edge_engine.engine.signal import (
    LOW_LIQUIDITY,
    MARKET_INELIGIBLE,
    SIGNAL_VALID,
    SPREAD_TOO_SMALL,
    score_signal,
)


class TestOrderbookParsing(unittest.TestCase):
    """Test orderbook parsing with flat and nested shapes."""

    def test_parse_orderbook_flat_shape(self) -> None:
        """Test parsing flat orderbook shape (top-level yes/no)."""
        orderbook = {
            "yes": [[96, 250], [95, 100], [94, 50]],
            "no": [[5, 100], [4, 75], [3, 50]],
        }

        parsed = parse_orderbook(orderbook)

        # YES: best bid should be 96 (highest)
        assert parsed.bid_yes is not None
        assert parsed.bid_yes.price == 96.0
        assert parsed.bid_yes.size == 250

        # NO: best bid should be 5 (highest)
        assert parsed.bid_no is not None
        assert parsed.bid_no.price == 5.0
        assert parsed.bid_no.size == 100

        # Cross prices: cross_yes = 100 - best_no_bid, cross_no = 100 - best_yes_bid
        assert parsed.cross_yes is not None
        assert parsed.cross_yes.price == 95.0  # 100 - 5
        assert parsed.cross_no is not None
        assert parsed.cross_no.price == 4.0  # 100 - 96

    def test_parse_orderbook_nested_shape(self) -> None:
        """Test parsing nested orderbook shape (orderbook.orderbook.yes/no)."""
        orderbook = {
            "orderbook": {
                "yes": [[96, 250], [95, 100]],
                "no": [[5, 100], [4, 75]],
            }
        }

        parsed = parse_orderbook(orderbook)

        assert parsed.bid_yes is not None
        assert parsed.bid_yes.price == 96.0
        assert parsed.bid_no is not None
        assert parsed.bid_no.price == 5.0
        assert parsed.cross_yes is not None
        assert parsed.cross_yes.price == 95.0

    def test_parse_orderbook_empty(self) -> None:
        """Test parsing empty orderbook."""
        orderbook: dict[str, list] = {"yes": [], "no": []}
        parsed = parse_orderbook(orderbook)

        assert parsed.bid_yes is None
        assert parsed.bid_no is None
        assert parsed.cross_yes is None
        assert parsed.cross_no is None

    def test_parse_orderbook_missing_side(self) -> None:
        """Test parsing orderbook with only YES side."""
        orderbook = {
            "yes": [[96, 250], [95, 100]],
        }
        parsed = parse_orderbook(orderbook)

        assert parsed.bid_yes is not None
        assert parsed.bid_yes.price == 96.0
        # NO bid missing, so cross_yes cannot be computed
        assert parsed.cross_yes is None
        assert parsed.bid_no is None

    def test_parse_orderbook_malformed(self) -> None:
        """Test parsing with malformed data (graceful degradation)."""
        orderbook = {
            "yes": [[96, 250], "invalid", [94, 50]],
            "no": [[5, 100]],
        }
        parsed = parse_orderbook(orderbook)

        # Should extract valid entries, skip invalid
        assert parsed.bid_yes is not None
        assert parsed.bid_yes.price == 96.0
        assert parsed.bid_no is not None
        assert parsed.bid_no.price == 5.0
        assert parsed.cross_yes is not None
        assert parsed.cross_yes.price == 95.0

    def test_get_inside_width_valid(self) -> None:
        """Test inside width calculation with valid levels."""
        width = get_inside_width(bid=80.0, cross=85.0)
        assert width == 5.0

    def test_get_inside_width_none(self) -> None:
        """Test inside width with missing price."""
        width = get_inside_width(None, 85.0)
        assert width is None

        width = get_inside_width(80.0, None)
        assert width is None

    def test_get_inside_width_zero_or_negative(self) -> None:
        """Test inside width with zero or negative room (no room to quote)."""
        width = get_inside_width(bid=85.0, cross=85.0)
        assert width is None  # Exactly at cross, no room

        width = get_inside_width(bid=85.0, cross=80.0)
        assert width is None  # Cross below bid, inverted

    def test_get_best_bid_size(self) -> None:
        """Test liquidity extraction."""
        level = BestLevel(price=96.0, size=250)
        size = get_best_bid_size(level)
        assert size == 250

        size_none = get_best_bid_size(None)
        assert size_none == 0


class TestSignalScoring(unittest.TestCase):
    """Test signal scoring logic with inside widths."""

    def test_signal_valid_edge_both_sides(self) -> None:
        """Test signal with valid inside width and edge on both sides."""
        orderbook = {
            "yes": [[80, 250], [79, 100]],
            "no": [[15, 250], [14, 100]],
        }
        # bid_yes=80, cross_yes=100-15=85, inside_width_yes=5
        # bid_no=15, cross_no=100-80=20, inside_width_no=5
        # net_edges: 5 - 0.5 - 0.1 = 4.4 >= 0.1 ✓

        signal = score_signal("TEST-TICKER", orderbook, fee_estimate=0.5, min_edge=0.1)

        assert signal.ticker == "TEST-TICKER"
        assert signal.reason_code == SIGNAL_VALID
        assert signal.inside_width_yes == 5.0
        assert signal.inside_width_no == 5.0
        assert signal.net_edge_yes is not None
        assert signal.net_edge_yes >= 0.1

    def test_signal_valid_with_safety_buffer(self) -> None:
        """Test that safety buffer is applied to net edge calculation."""
        orderbook = {
            "yes": [[80, 250]],
            "no": [[19, 250]],
        }
        # inside_width_yes = 100-19-80 = 1.0
        # net_edge before buffer = 1.0 - 0.5 = 0.5
        # net_edge with buffer = 1.0 - 0.5 - 0.2 = 0.3 >= 0.1 ✓

        signal = score_signal(
            "TEST-TICKER", orderbook, fee_estimate=0.5, min_edge=0.1, safety_buffer=0.2
        )

        assert signal.reason_code == SIGNAL_VALID
        assert signal.inside_width_yes == 1.0
        assert signal.net_edge_yes == 0.3

    def test_signal_spread_too_small(self) -> None:
        """Test signal where inside width is too narrow."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[49.9, 100]],
        }
        # inside_width_yes = 100-49.9-50 = 0.1
        # inside_width_no = 100-50-49.9 = 0.1
        # net_edge = 0.1 - 0.5 - 0.1 = -0.5 < 0.1 → SPREAD_TOO_SMALL

        signal = score_signal("TEST-TICKER", orderbook, fee_estimate=0.5, min_edge=0.1)

        assert signal.reason_code == SPREAD_TOO_SMALL

    def test_signal_low_liquidity(self) -> None:
        """Test signal with low liquidity at best bid."""
        orderbook = {
            "yes": [[80, 5], [79, 100]],
            "no": [[15, 5], [14, 100]],
        }
        # top_liquidity = max(5, 5) = 5 < min_liquidity (10)

        signal = score_signal(
            "TEST-TICKER", orderbook, fee_estimate=0.5, min_liquidity=10, min_edge=0.1
        )

        assert signal.reason_code == LOW_LIQUIDITY
        assert signal.top_liquidity == 5

    def test_signal_market_ineligible_no_bids(self) -> None:
        """Test signal with missing best bids."""
        orderbook: dict = {}

        signal = score_signal("TEST-TICKER", orderbook, fee_estimate=0.5)

        assert signal.reason_code == MARKET_INELIGIBLE

    def test_signal_market_ineligible_one_side_missing(self) -> None:
        """Test signal where one side is missing (cannot compute cross/inside width)."""
        orderbook = {
            "yes": [[80, 100]],
        }

        signal = score_signal("TEST-TICKER", orderbook, fee_estimate=0.5)

        # Without NO side, can't compute cross_yes, so no inside width available
        assert signal.reason_code == SPREAD_TOO_SMALL  # No valid widths to trade
        assert signal.bid_yes == 80.0
        assert signal.cross_yes is None
        assert signal.bid_no is None

    def test_signal_inverted_market(self) -> None:
        """Test signal where best bids invert (no inside width)."""
        orderbook = {
            "yes": [[60, 500]],
            "no": [[50, 500]],
        }
        # bid_yes=60, cross_yes=100-50=50, inside_width_yes=negative (invalid)
        # bid_no=50, cross_no=100-60=40, inside_width_no=negative (invalid)

        signal = score_signal("TEST-TICKER", orderbook, fee_estimate=0.5, min_edge=0.1)

        assert signal.reason_code == SPREAD_TOO_SMALL  # No valid inside widths

    def test_signal_reason_codes_valid_set(self) -> None:
        """Test that all emitted reason codes are in valid set."""
        orderbook = {
            "yes": [[80, 250]],
            "no": [[15, 250]],
        }

        for fee in [0.1, 0.5, 1.0, 2.0]:
            signal = score_signal("TEST-TICKER", orderbook, fee_estimate=fee)
            assert signal.reason_code in {
                SPREAD_TOO_SMALL,
                LOW_LIQUIDITY,
                MARKET_INELIGIBLE,
                SIGNAL_VALID,
            }

    def test_signal_timestamp_recent(self) -> None:
        """Test that signal timestamp is recent."""
        orderbook = {"yes": [[96, 250]], "no": [[5, 100]]}

        before_ms = int(time.time() * 1000)
        signal = score_signal("TEST-TICKER", orderbook)
        after_ms = int(time.time() * 1000)

        assert before_ms <= signal.timestamp_ms <= after_ms
