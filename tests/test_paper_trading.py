"""Tests for paper trading module."""

import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.engine.paper import (
    QuoteIntent,
    build_quote_intents,
    simulate_fill,
)
from kalshi_edge_engine.engine.signal import SIGNAL_VALID, SPREAD_TOO_SMALL, Signal


@pytest.fixture
def sample_valid_signal() -> Signal:
    """Create a sample SIGNAL_VALID for testing."""
    now_ms = int(time.time() * 1000)
    return Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SIGNAL_VALID,
        net_edge_yes=0.5,
        net_edge_no=0.3,
        inside_width_yes=1.5,
        inside_width_no=1.2,
        bid_yes=50.0,
        cross_yes=52.0,
        bid_no=45.0,
        cross_no=55.0,
        bid_sum=95.0,
        imbalance_gap=5.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
    )


@pytest.fixture
def sample_invalid_signal() -> Signal:
    """Create a sample SPREAD_TOO_SMALL signal for testing."""
    now_ms = int(time.time() * 1000)
    return Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SPREAD_TOO_SMALL,
        net_edge_yes=None,
        net_edge_no=None,
        inside_width_yes=None,
        inside_width_no=None,
        bid_yes=None,
        cross_yes=None,
        bid_no=None,
        cross_no=None,
        bid_sum=None,
        imbalance_gap=None,
        fee_yes=None,
        fee_no=None,
        fee_exit_yes=None,
        fee_exit_no=None,
        total_fee_yes=None,
        total_fee_no=None,
        fee_mode="maker",
        round_trip=False,
        liq_yes=5,
        liq_no=5,
        top_liquidity=5,
        timestamp_ms=now_ms,
    )


class TestBuildQuoteIntents:
    """Tests for build_quote_intents function."""

    def test_no_intents_for_invalid_signal(self, sample_invalid_signal: Signal) -> None:
        """Test that no intents are created for non-SIGNAL_VALID signals."""
        intents = build_quote_intents(sample_invalid_signal)
        assert len(intents) == 0

    def test_creates_intents_for_valid_signal(self, sample_valid_signal: Signal) -> None:
        """Test that intents are created for SIGNAL_VALID with valid net edges."""
        intents = build_quote_intents(sample_valid_signal)
        assert len(intents) == 2  # Both YES and NO sides should generate intents

    def test_intent_properties(self, sample_valid_signal: Signal) -> None:
        """Test that intents have correct properties."""
        intents = build_quote_intents(sample_valid_signal)

        # Should have both YES and NO intents
        yes_intents = [i for i in intents if i.side == "YES"]
        no_intents = [i for i in intents if i.side == "NO"]

        assert len(yes_intents) == 1
        assert len(no_intents) == 1

        # Check YES intent
        yes_intent = yes_intents[0]
        assert yes_intent.ticker == sample_valid_signal.ticker
        assert yes_intent.side == "YES"
        assert yes_intent.size_contracts == 1
        assert 1.0 <= yes_intent.quote_price_cents <= 99.0
        assert yes_intent.quote_price_cents > sample_valid_signal.bid_yes
        assert yes_intent.net_edge == sample_valid_signal.net_edge_yes
        assert yes_intent.fee_mode == sample_valid_signal.fee_mode
        assert yes_intent.round_trip == sample_valid_signal.round_trip

    def test_quote_price_bounded(self, sample_valid_signal: Signal) -> None:
        """Test that quote prices are bounded between 1 and 99."""
        intents = build_quote_intents(sample_valid_signal)
        for intent in intents:
            assert 1.0 <= intent.quote_price_cents <= 99.0
            # Quote price should be above bid
            if intent.side == "YES":
                assert intent.quote_price_cents > sample_valid_signal.bid_yes
            else:
                assert intent.quote_price_cents > sample_valid_signal.bid_no

    def test_no_intents_if_edge_too_small(self, sample_valid_signal: Signal) -> None:
        """Test that no intents created if net edge below threshold."""
        sample_valid_signal.net_edge_yes = 0.05  # Below default min_edge of 0.1
        sample_valid_signal.net_edge_no = 0.05
        intents = build_quote_intents(sample_valid_signal, min_edge=0.1)
        assert len(intents) == 0

    def test_only_yes_intent_if_no_side_ineligible(self, sample_valid_signal: Signal) -> None:
        """Test that only YES intent is created if NO side is ineligible."""
        sample_valid_signal.net_edge_no = None
        sample_valid_signal.inside_width_no = None
        intents = build_quote_intents(sample_valid_signal)
        assert len(intents) == 1
        assert intents[0].side == "YES"

    def test_intent_id_is_unique(self, sample_valid_signal: Signal) -> None:
        """Test that each intent gets a unique ID."""
        intents1 = build_quote_intents(sample_valid_signal)
        intents2 = build_quote_intents(sample_valid_signal)

        all_ids = [i.id for i in intents1 + intents2]
        assert len(all_ids) == len(set(all_ids))  # All IDs should be unique


@pytest.mark.asyncio
class TestSimulateFill:
    """Tests for simulate_fill async function."""

    @pytest.mark.asyncio
    async def test_fill_detected_when_bid_exceeds_quote(self, sample_valid_signal: Signal) -> None:
        """Test that fill is detected when best bid >= quote price."""
        intent = QuoteIntent(
            id="test-intent-1",
            ticker=sample_valid_signal.ticker,
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=int(time.time() * 1000),
            net_edge=0.5,
            inside_width=1.5,
            imbalance_gap=5.0,
            fee_mode="maker",
            round_trip=True,
        )

        # Mock client to return orderbook with YES bid >= quote price
        # Flat format: yes/no -> [[price, size], [price, size], ...]
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(
            return_value={
                "yes": [[51.0, 100]],  # YES bid is 51, above quote of 50
                "no": [[45.0, 100]],
            }
        )

        fill, outcome = await simulate_fill(mock_client, intent, horizon_seconds=5)

        assert fill.filled is True
        assert fill.intent_id == intent.id
        assert outcome.pnl_cents is not None
        assert outcome.note == "filled"

    @pytest.mark.asyncio
    async def test_timeout_when_no_fill(self, sample_valid_signal: Signal) -> None:
        """Test that fill times out when bid never exceeds quote."""
        intent = QuoteIntent(
            id="test-intent-2",
            ticker=sample_valid_signal.ticker,
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=int(time.time() * 1000),
            net_edge=0.5,
            inside_width=1.5,
            imbalance_gap=5.0,
            fee_mode="maker",
            round_trip=True,
        )

        # Mock client to return orderbook with YES bid < quote price
        # Flat format: yes/no -> [[price, size], [price, size], ...]
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(
            return_value={
                "yes": [[49.0, 100]],  # YES bid is 49, below quote of 50
                "no": [[45.0, 100]],
            }
        )

        fill, outcome = await simulate_fill(mock_client, intent, horizon_seconds=1)

        assert fill.filled is False
        assert fill.time_to_fill_ms is not None
        assert outcome.pnl_cents == 0.0
        assert outcome.note == "timeout"

    @pytest.mark.asyncio
    async def test_fill_no_side(self, sample_valid_signal: Signal) -> None:
        """Test fill detection for NO side."""
        intent = QuoteIntent(
            id="test-intent-3",
            ticker=sample_valid_signal.ticker,
            side="NO",
            quote_price_cents=45.0,
            size_contracts=1,
            created_ts_ms=int(time.time() * 1000),
            net_edge=0.3,
            inside_width=1.2,
            imbalance_gap=5.0,
            fee_mode="maker",
            round_trip=True,
        )

        # Flat format: yes/no -> [[price, size], [price, size], ...]
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(
            return_value={
                "yes": [[50.0, 100]],
                "no": [[46.0, 100]],  # NO bid is 46, above quote of 45
            }
        )

        fill, outcome = await simulate_fill(mock_client, intent, horizon_seconds=5)

        assert fill.filled is True
        assert outcome.note == "filled"

    @pytest.mark.asyncio
    async def test_pnl_computation(self, sample_valid_signal: Signal) -> None:
        """Test that PnL is computed correctly on fill."""
        intent = QuoteIntent(
            id="test-intent-4",
            ticker=sample_valid_signal.ticker,
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=int(time.time() * 1000),
            net_edge=0.5,
            inside_width=1.5,
            imbalance_gap=5.0,
            fee_mode="maker",
            round_trip=True,
        )

        # Entry at 50, exit mark at 55 (cross price)
        # Flat format: yes/no -> [[price, size], [price, size], ...]
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(
            return_value={
                "yes": [[51.0, 100]],  # YES bid at 51
                "no": [[45.0, 100]],  # NO bid at 45, so cross_yes = 100 - 45 = 55
            }
        )

        fill, outcome = await simulate_fill(mock_client, intent, horizon_seconds=5)

        # PnL should be: (exit - entry) * size - fees
        # With mocked fees, should be positive if exit > entry
        assert outcome.pnl_cents is not None
        assert outcome.total_fees_cents >= 0.0
        assert outcome.pnl_dollars == outcome.pnl_cents / 100.0

    @pytest.mark.asyncio
    async def test_error_handling(self, sample_valid_signal: Signal) -> None:
        """Test that errors are handled gracefully."""
        intent = QuoteIntent(
            id="test-intent-5",
            ticker=sample_valid_signal.ticker,
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=int(time.time() * 1000),
            net_edge=0.5,
            inside_width=1.5,
            imbalance_gap=5.0,
            fee_mode="maker",
            round_trip=True,
        )

        # Mock client that raises an error
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(side_effect=Exception("API error"))

        fill, outcome = await simulate_fill(mock_client, intent, horizon_seconds=1)

        assert fill.filled is False
        # Outcome note could be either "timeout" or contain "error" - just check it's not successful
        assert outcome.note in ("timeout", "error: API error")
        assert outcome.pnl_cents == 0.0
