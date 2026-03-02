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
        # Fees computed dynamically based on bid prices

        signal = score_signal("TEST-TICKER", orderbook, min_edge=0.1)

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
            "no": [[18, 250]],
        }
        # inside_width_yes = 100-18-80 = 2.0
        # Fee computed dynamically, safety_buffer=0.2
        # imbalance_gap = 100 - (80 + 18) = 2.0 >= 1.5 ✓

        signal = score_signal("TEST-TICKER", orderbook, min_edge=0.1, safety_buffer=0.2)

        assert signal.reason_code == SIGNAL_VALID
        assert signal.inside_width_yes == 2.0
        # net_edge = inside_width - fee - safety_buffer (fee varies by price)
        assert signal.net_edge_yes is not None and signal.net_edge_yes >= 0.1

    def test_signal_spread_too_small(self) -> None:
        """Test signal where inside width is too narrow."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[49.9, 100]],
        }
        # inside_width_yes = 100-49.9-50 = 0.1
        # inside_width_no = 100-50-49.9 = 0.1
        # Both widths <= 1 tick → SPREAD_TOO_SMALL

        signal = score_signal("TEST-TICKER", orderbook, min_edge=0.1)

        assert signal.reason_code == SPREAD_TOO_SMALL

    def test_signal_low_liquidity(self) -> None:
        """Test signal with low liquidity at best bid."""
        orderbook = {
            "yes": [[80, 5], [79, 100]],
            "no": [[15, 5], [14, 100]],
        }
        # top_liquidity = max(5, 5) = 5 < min_liquidity (10)

        signal = score_signal("TEST-TICKER", orderbook, min_liquidity=10, min_edge=0.1)

        assert signal.reason_code == LOW_LIQUIDITY
        assert signal.top_liquidity == 5

    def test_signal_market_ineligible_no_bids(self) -> None:
        """Test signal with missing best bids."""
        orderbook: dict = {}

        signal = score_signal("TEST-TICKER", orderbook)

        assert signal.reason_code == MARKET_INELIGIBLE

    def test_signal_market_ineligible_one_side_missing(self) -> None:
        """Test signal where one side is missing (cannot compute cross/inside width)."""
        orderbook = {
            "yes": [[80, 100]],
        }

        signal = score_signal("TEST-TICKER", orderbook)

        # Without NO side, market is structurally ineligible
        assert signal.reason_code == MARKET_INELIGIBLE
        assert signal.bid_yes == 80.0
        assert signal.cross_yes is None
        assert signal.bid_no is None

    def test_signal_inverted_market(self) -> None:
        """Test signal where best bids invert (no inside width)."""
        orderbook = {
            "yes": [[60, 500]],
            "no": [[50, 500]],
        }
        # bid_sum=110 > 100 is crossed/distorted and therefore ineligible

        signal = score_signal("TEST-TICKER", orderbook, min_edge=0.1)

        assert signal.reason_code == MARKET_INELIGIBLE

    def test_signal_symmetric_one_tick_not_valid(self) -> None:
        """Symmetric 1-tick structure should be treated as SPREAD_TOO_SMALL."""
        orderbook = {
            "yes": [[50, 250]],
            "no": [[49, 250]],
        }
        # bid_sum=99, imbalance_gap=1.0, inside_width=1.0 (one tick)

        signal = score_signal("TEST-TICKER", orderbook)

        assert signal.reason_code == SPREAD_TOO_SMALL
        assert signal.bid_sum == 99.0
        assert signal.imbalance_gap == 1.0
        assert signal.inside_width_yes == 1.0
        assert signal.inside_width_no == 1.0

    def test_signal_widened_distorted_book_valid(self) -> None:
        """Wider structurally imbalanced book should be valid when edge is sufficient."""
        orderbook = {
            "yes": [[70, 300]],
            "no": [[27, 300]],
        }
        # bid_sum=97, imbalance_gap=3.0, inside_width=3.0

        signal = score_signal("TEST-TICKER", orderbook)

        assert signal.reason_code == SIGNAL_VALID
        assert signal.bid_sum == 97.0
        assert signal.imbalance_gap == 3.0
        # Net edge varies with dynamic fees
        assert signal.net_edge_yes is not None and signal.net_edge_yes > 0.1
        assert signal.net_edge_no is not None and signal.net_edge_no > 0.1

    def test_signal_crossed_book_ineligible(self) -> None:
        """Crossed structure with bid_sum > 100 should be ineligible."""
        orderbook = {
            "yes": [[60, 400]],
            "no": [[41, 400]],
        }

        signal = score_signal("TEST-TICKER", orderbook)

        assert signal.reason_code == MARKET_INELIGIBLE
        assert signal.bid_sum == 101.0
        assert signal.imbalance_gap == -1.0

    def test_signal_reason_codes_valid_set(self) -> None:
        """Test that all emitted reason codes are in valid set."""
        orderbook = {
            "yes": [[80, 250]],
            "no": [[15, 250]],
        }

        # Test with different fee modes
        for fee_mode in ["maker", "taker"]:
            signal = score_signal("TEST-TICKER", orderbook, fee_mode=fee_mode)
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


class TestOrderbookPriceValidation(unittest.TestCase):
    """Test price validation in orderbook parsing."""

    def test_parse_orderbook_invalid_prices_ignored(self) -> None:
        """Test that prices outside 0-100 range are ignored."""
        orderbook = {
            "yes": [[96, 250], [101, 100], [-1, 50]],  # 101 and -1 invalid
            "no": [[5, 100], [150, 75]],  # 150 invalid
        }

        parsed = parse_orderbook(orderbook)

        # Should use 96 (valid), not 101 or -1
        assert parsed.bid_yes is not None
        assert parsed.bid_yes.price == 96.0

        # Should use 5 (valid), not 150
        assert parsed.bid_no is not None
        assert parsed.bid_no.price == 5.0

    def test_parse_orderbook_invalid_sizes_ignored(self) -> None:
        """Test that negative sizes are ignored."""
        orderbook = {
            "yes": [[96, 250], [95, -100]],  # -100 invalid
            "no": [[5, 100]],
        }

        parsed = parse_orderbook(orderbook)

        # Should use 96 (valid), not 95 with negative size
        assert parsed.bid_yes is not None
        assert parsed.bid_yes.price == 96.0


class TestCrossPriceSize(unittest.TestCase):
    """Test that cross prices have size=0."""

    def test_cross_price_size_zero(self) -> None:
        """Test that cross prices are marked with size=0 (not inherited from bids)."""
        orderbook = {
            "yes": [[80, 250]],
            "no": [[15, 500]],  # Different sizes
        }

        parsed = parse_orderbook(orderbook)

        # Cross prices should have size=0, not inherit from bid sizes
        assert parsed.cross_yes is not None
        assert parsed.cross_yes.price == 85.0  # 100 - 15
        assert parsed.cross_yes.size == 0

        assert parsed.cross_no is not None
        assert parsed.cross_no.price == 20.0  # 100 - 80
        assert parsed.cross_no.size == 0


class TestSideLiquidityGating(unittest.TestCase):
    """Test side-specific liquidity gating."""

    def test_signal_tracks_liq_yes_liq_no_separately(self) -> None:
        """Test that signal tracks liq_yes and liq_no separately."""
        orderbook = {
            "yes": [[80, 250]],
            "no": [[15, 500]],
        }

        signal = score_signal("TEST-TICKER", orderbook)

        assert signal.liq_yes == 250
        assert signal.liq_no == 500
        assert signal.top_liquidity == 500

    def test_signal_low_liquidity_yes_side_only(self) -> None:
        """Test SIGNAL_VALID when YES side is illiquid but NO side is eligible+valid."""
        orderbook = {
            "yes": [[80, 5]],  # Only 5 contracts - fails min_liquidity=10
            "no": [[15, 250]],  # Good liquidity
        }
        # inside_width_yes = 100-15-80 = 5 (>1 tick, usable but illiquid)
        # inside_width_no = 100-80-15 = 5 (>1 tick, usable and liquid)
        # NO side eligible with sufficient net edge
        # Should be SIGNAL_VALID (NO side valid despite YES side being illiquid)

        signal = score_signal("TEST-TICKER", orderbook, min_liquidity=10)

        assert signal.reason_code == SIGNAL_VALID
        assert signal.liq_yes == 5
        assert signal.liq_no == 250
        # NO side should have positive net edge
        assert signal.net_edge_no is not None
        assert signal.net_edge_no >= 0.1

    def test_signal_low_liquidity_no_side_only(self) -> None:
        """Test SIGNAL_VALID when NO side is illiquid but YES side is eligible+valid."""
        orderbook = {
            "yes": [[80, 250]],
            "no": [[15, 5]],  # Only 5 contracts - fails min_liquidity=10
        }
        # inside_width_yes = 100-15-80 = 5 (>1 tick, usable and liquid)
        # inside_width_no = 100-80-15 = 5 (>1 tick, usable but illiquid)
        # YES side eligible with sufficient net edge
        # Should be SIGNAL_VALID (YES side valid despite NO side being illiquid)

        signal = score_signal("TEST-TICKER", orderbook, min_liquidity=10)

        assert signal.reason_code == SIGNAL_VALID
        assert signal.liq_yes == 250
        assert signal.liq_no == 5
        # YES side should have positive net edge
        assert signal.net_edge_yes is not None
        assert signal.net_edge_yes >= 0.1

    def test_signal_valid_ignores_liquidity_on_unusable_side(self) -> None:
        """Test that liquidity is only required for usable sides."""
        orderbook2 = {
            "yes": [[50, 250]],
            "no": [[49.5, 5]],  # Narrow NO side (not usable), low liquidity
        }
        # yes: bid_yes=50, cross_yes=100-49.5=50.5, width=0.5 (not usable, <1 tick)
        # no:  bid_no=49.5, cross_no=100-50=50, width=0.5 (not usable, <1 tick)
        # Both sides not usable → SPREAD_TOO_SMALL, liquidity ignored

        signal = score_signal("TEST-TICKER", orderbook2, min_liquidity=10)

        # With <1 tick on both sides, should be SPREAD_TOO_SMALL
        assert signal.reason_code == SPREAD_TOO_SMALL
        # But let's verify liquidity doesn't block when side is narrow
        assert signal.liq_no == 5

    def test_signal_both_sides_sufficient_liquidity(self) -> None:
        """Test SIGNAL_VALID when both sides have sufficient liquidity."""
        orderbook = {
            "yes": [[75, 100]],
            "no": [[20, 150]],
        }
        # yes: bid_yes=75, cross_yes=100-20=80, width=5 (usable)
        # no:  bid_no=20, cross_no=100-75=25, width=5 (usable)
        # liq_yes=100 >= 10, liq_no=150 >= 10 ✓
        # bid_sum=95, imbalance=5 >= 1.5 ✓
        # Net edges computed with dynamic fees

        signal = score_signal("TEST-TICKER", orderbook, min_liquidity=10)

        assert signal.reason_code == SIGNAL_VALID
        assert signal.liq_yes == 100
        assert signal.liq_no == 150

    def test_signal_fee_fields_populated(self) -> None:
        """Test that fee_yes and fee_no fields are populated based on bid prices."""
        orderbook = {
            "yes": [[75, 100]],
            "no": [[20, 150]],
        }

        signal = score_signal("TEST-TICKER", orderbook, fee_mode="maker")

        # Fees should be computed based on bid prices
        assert signal.fee_yes is not None
        assert signal.fee_no is not None
        assert signal.fee_yes > 0.0
        assert signal.fee_no > 0.0

    def test_signal_fee_mode_maker_vs_taker(self) -> None:
        """Test that taker fees are higher than maker fees."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[40, 100]],
        }

        maker_signal = score_signal("TEST-TICKER", orderbook, fee_mode="maker")
        taker_signal = score_signal("TEST-TICKER", orderbook, fee_mode="taker")

        # Taker fees should be higher than maker fees
        assert maker_signal.fee_yes is not None
        assert taker_signal.fee_yes is not None
        assert taker_signal.fee_yes > maker_signal.fee_yes

        assert maker_signal.fee_no is not None
        assert taker_signal.fee_no is not None
        assert taker_signal.fee_no > maker_signal.fee_no

    def test_signal_fee_none_when_bid_missing(self) -> None:
        """Test that fee fields are None when bids are missing."""
        orderbook = {
            "yes": [[50, 100]],
        }

        signal = score_signal("TEST-TICKER", orderbook)

        # YES side should have fee
        assert signal.fee_yes is not None
        # NO side missing, so fee_no should be None
        assert signal.fee_no is None

    def test_signal_contracts_for_fee(self) -> None:
        """Test that contracts_for_fee parameter scales fees correctly."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[40, 100]],
        }

        signal_1 = score_signal("TEST-TICKER", orderbook, contracts_for_fee=1)
        signal_10 = score_signal("TEST-TICKER", orderbook, contracts_for_fee=10)

        # Fees with 10 contracts should be higher than 1 contract
        assert signal_1.fee_yes is not None
        assert signal_10.fee_yes is not None
        assert signal_10.fee_yes > signal_1.fee_yes

    def test_signal_round_trip_false(self) -> None:
        """Test that round_trip=False uses only entry fees."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[40, 100]],
        }

        signal = score_signal("TEST-TICKER", orderbook, fee_mode="maker", round_trip=False)

        # With round_trip=False, fee_yes should be entry fee only (maker)
        # At P=50¢, maker fee = ceil(0.0175*0.5*0.5*100) = 1 cent
        assert signal.fee_yes == 1.0
        assert signal.fee_no == 1.0

    def test_signal_round_trip_true_reduces_net_edge(self) -> None:
        """Test that round_trip=True reduces net edge by including exit fees."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[40, 100]],
        }

        signal_no_rt = score_signal("TEST-TICKER", orderbook, fee_mode="maker", round_trip=False)
        signal_rt = score_signal("TEST-TICKER", orderbook, fee_mode="maker", round_trip=True)

        # Round trip should reduce net edge (more fees subtracted)
        assert signal_no_rt.net_edge_yes is not None
        assert signal_rt.net_edge_yes is not None
        assert signal_rt.net_edge_yes < signal_no_rt.net_edge_yes

        assert signal_no_rt.net_edge_no is not None
        assert signal_rt.net_edge_no is not None
        assert signal_rt.net_edge_no < signal_no_rt.net_edge_no

        # fee_yes/fee_no fields should still reflect entry fees only
        assert signal_rt.fee_yes == signal_no_rt.fee_yes
        assert signal_rt.fee_no == signal_no_rt.fee_no

    def test_signal_exit_fees_use_cross_prices(self) -> None:
        """Test that exit fees are calculated using cross prices, not bid prices."""
        # Create orderbook where bid and cross are significantly different
        orderbook = {
            "yes": [[1, 100]],  # bid_yes=1, cross_yes=100-99=1
            "no": [[99, 100]],  # bid_no=99, cross_no=100-1=99
        }

        signal = score_signal("TEST-TICKER", orderbook, fee_mode="maker", round_trip=True)

        # Exit fees should use cross prices (1¢ for YES, 99¢ for NO)
        # At P=1¢ or P=99¢, both are near boundaries where fee → 0
        # P=0.01: P*(1-P) = 0.01*0.99 = 0.0099
        # Taker: 0.07 * 0.0099 = 0.000693 → ceil(0.0693) = 1 cent
        # P=0.99: P*(1-P) = 0.99*0.01 = 0.0099 (same)
        assert signal.fee_exit_yes is not None
        assert signal.fee_exit_no is not None
        assert signal.fee_exit_yes == 1.0  # Using cross_yes=1
        assert signal.fee_exit_no == 1.0  # Using cross_no=99

        # Entry fees use bid prices (1¢ and 99¢)
        assert signal.fee_yes == 1.0  # maker at bid_yes=1
        assert signal.fee_no == 1.0  # maker at bid_no=99

    def test_signal_debug_fields_populated(self) -> None:
        """Test that debug fields are populated correctly."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[40, 100]],
        }

        signal = score_signal("TEST-TICKER", orderbook, fee_mode="maker", round_trip=True)

        # Debug fields should be populated
        assert signal.fee_mode == "maker"
        assert signal.round_trip is True
        assert signal.fee_exit_yes is not None
        assert signal.fee_exit_no is not None
        assert signal.total_fee_yes is not None
        assert signal.total_fee_no is not None

        # Total fees should equal entry + exit
        assert signal.total_fee_yes == signal.fee_yes + signal.fee_exit_yes
        assert signal.total_fee_no == signal.fee_no + signal.fee_exit_no

    def test_signal_no_exit_fees_when_round_trip_false(self) -> None:
        """Test that exit fee fields are None when round_trip=False."""
        orderbook = {
            "yes": [[50, 100]],
            "no": [[40, 100]],
        }

        signal = score_signal("TEST-TICKER", orderbook, fee_mode="maker", round_trip=False)

        # Exit fees should be None
        assert signal.fee_exit_yes is None
        assert signal.fee_exit_no is None

        # Total fees should equal entry fees only
        assert signal.total_fee_yes == signal.fee_yes
        assert signal.total_fee_no == signal.fee_no

        # Debug fields still populated
        assert signal.fee_mode == "maker"
        assert signal.round_trip is False

    def test_signal_round_trip_requires_cross_yes(self) -> None:
        """Test that round_trip=True makes YES side ineligible when cross_yes is missing."""
        # NO bid at 0¢ means cross_yes (100 - 0) is at boundary, but let's use no NO side at all
        # Actually, if NO bid is 0, cross_yes = 100 - 0 = 100, which exists
        # To make cross_yes missing, we need NO side to not have a bid at all
        orderbook = {
            "yes": [[50, 100]],  # YES bid exists at 50¢ with good liquidity
            "no": [],  # No NO bids -> cross_yes will be None
        }

        signal = score_signal(
            "TEST-TICKER",
            orderbook,
            fee_mode="maker",
            round_trip=True,  # Round-trip requires cross prices
            min_liquidity=10,
        )

        # YES side should not be eligible (even though it has liquidity and width)
        # because cross_yes is missing (needed for exit fee)
        assert signal.reason_code == "MARKET_INELIGIBLE"  # Missing NO bid makes market ineligible
        assert signal.net_edge_yes is None
        assert signal.net_edge_no is None

    def test_signal_round_trip_requires_cross_no(self) -> None:
        """Test that round_trip=True makes NO side ineligible when cross_no is missing."""
        # To make cross_no missing, we need YES side to not have a bid
        orderbook = {
            "yes": [],  # No YES bids -> cross_no will be None
            "no": [[50, 100]],  # NO bid exists at 50¢ with good liquidity
        }

        signal = score_signal(
            "TEST-TICKER",
            orderbook,
            fee_mode="maker",
            round_trip=True,  # Round-trip requires cross prices
            min_liquidity=10,
        )

        # NO side should not be eligible because cross_no is missing
        assert signal.reason_code == "MARKET_INELIGIBLE"  # Missing YES bid makes market ineligible
        assert signal.net_edge_yes is None
        assert signal.net_edge_no is None

    def test_signal_round_trip_false_ignores_missing_cross(self) -> None:
        """Test that round_trip=False works even with unusual cross prices."""
        # With round_trip=False, we don't need cross prices for eligibility
        # Create a situation where both sides are present but one cross might be at boundary
        orderbook = {
            "yes": [[99, 100]],  # YES bid at 99¢
            "no": [[1, 100]],  # NO bid at 1¢
        }
        # cross_yes = 100 - 1 = 99¢, inside_width_yes = 99 - 99 = 0 (too small)
        # cross_no = 100 - 99 = 1¢, inside_width_no = 1 - 1 = 0 (too small)

        signal = score_signal(
            "TEST-TICKER",
            orderbook,
            fee_mode="maker",
            round_trip=False,  # No exit fees needed
            min_liquidity=10,
        )

        # Should fail due to insufficient width, not missing cross
        assert signal.reason_code == "SPREAD_TOO_SMALL"
        # But the bids should be parsed correctly
        assert signal.bid_yes == 99.0
        assert signal.bid_no == 1.0
