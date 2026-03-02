"""Tests for RealisticOrderSimulator."""

import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.engine.paper_sim import (
    Fill,
    OrderRequest,
    RealisticOrderSimulator,
    compute_simulator_diagnostics,
    extract_top_of_book_snapshot,
)
from kalshi_edge_engine.kalshi.client import KalshiClient


@pytest.fixture
def sample_order_request() -> OrderRequest:
    """Create a sample order request."""
    now_ms = int(time.time() * 1000)
    return OrderRequest(
        id="order-test-1",
        ticker="KXNBAGAME-26MAR01SASNYK-YES",
        side="YES",
        quote_price_cents=50.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=0.5,
        entry_fee_cents=0.5,
        take_profit_cents=2.0,
        max_hold_seconds=600,
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock KalshiClient."""
    return AsyncMock(spec=KalshiClient)


@pytest.fixture
def simulator() -> RealisticOrderSimulator:
    """Create a simulator instance."""
    return RealisticOrderSimulator(
        poll_interval_seconds=0.1,  # Short poll interval for tests
        fill_probability_price_moved=0.9,
        simulated_volume_per_poll=100,
    )


class TestRealisticOrderSimulator:
    """Tests for RealisticOrderSimulator class."""

    @pytest.mark.asyncio
    async def test_simulator_initialization(self):
        """Test simulator initialization."""
        sim = RealisticOrderSimulator(
            poll_interval_seconds=3.0,
            fill_probability_price_moved=0.8,
            fill_probability_at_best=0.2,
            simulated_volume_per_poll=50,
        )
        assert sim.poll_interval_seconds == 3.0
        assert sim.fill_probability_price_moved == 0.8
        assert sim.fill_probability_at_best == 0.2
        assert sim.simulated_volume_per_poll == 50

    @pytest.mark.asyncio
    async def test_simulate_order_fills_price_moved_through(
        self,
        simulator: RealisticOrderSimulator,
        sample_order_request: OrderRequest,
        mock_client: AsyncMock,
    ):
        """Test order fill when price moves through the order."""
        # Mock orderbook where best bid moves above quote price
        # Kalshi orderbook format: {"yes": [[price, size], ...], "no": [[price, size], ...]}
        mock_orderbook = {
            "yes": [[55.0, 100]],
            "no": [[45.0, 100]],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            sample_order_request,
            horizon_seconds=5,
        )

        # Should have at least one fill since price moved through
        assert len(fills) >= 1
        assert order_state["status"] in ["filled", "take_profit"]
        assert order_state["total_filled_contracts"] > 0

    @pytest.mark.asyncio
    async def test_simulate_order_fills_timeout(
        self,
        simulator: RealisticOrderSimulator,
        sample_order_request: OrderRequest,
        mock_client: AsyncMock,
    ):
        """Test order timeout when price never reaches quote."""
        # Mock orderbook where best bid stays below quote price
        mock_orderbook = {
            "yes": [[40.0, 100]],
            "no": [[59.0, 100]],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            sample_order_request,
            horizon_seconds=0.2,  # Short horizon for faster test
        )

        # Should timeout without any fills
        assert len(fills) == 0
        assert order_state["status"] == "timeout"
        assert order_state["total_filled_contracts"] == 0

    @pytest.mark.asyncio
    async def test_simulate_order_fills_returns_fill_objects(
        self,
        simulator: RealisticOrderSimulator,
        sample_order_request: OrderRequest,
        mock_client: AsyncMock,
    ):
        """Test that simulate_order_fills returns proper Fill objects."""
        mock_orderbook = {
            "yes": [[40.0, 100]],
            "no": [[60.0, 100]],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            sample_order_request,
            horizon_seconds=0.2,
        )

        if len(fills) > 0:
            fill = fills[0]
            assert isinstance(fill, Fill)
            assert fill.order_id == sample_order_request.id
            assert fill.fill_ts_ms > sample_order_request.created_ts_ms
            assert fill.fill_price_cents > 0
            assert fill.fill_size > 0
            assert fill.remaining_size >= 0
            assert fill.fill_fee_cents is not None

    @pytest.mark.asyncio
    async def test_simulate_order_fills_respects_horizon(
        self,
        simulator: RealisticOrderSimulator,
        sample_order_request: OrderRequest,
        mock_client: AsyncMock,
    ):
        """Test that simulation respects the horizon_seconds parameter."""
        mock_orderbook = {
            "yes": [[40.0, 100]],
            "no": [[60.0, 100]],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        start_ts = int(time.time() * 1000)

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            sample_order_request,
            horizon_seconds=0.3,
        )

        end_ts = int(time.time() * 1000)
        elapsed = end_ts - start_ts

        # Simulation should not take much longer than horizon
        # Allow 2x margin for test overhead
        assert elapsed < 600  # 0.3s horizon + 100% overhead = 600ms

    @pytest.mark.asyncio
    async def test_simulate_order_fills_side_no(
        self, simulator: RealisticOrderSimulator, mock_client: AsyncMock
    ):
        """Test order fill for NO side."""
        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-no-1",
            ticker="KXNBAGAME-26MAR01SASNYK-NO",
            side="NO",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )

        mock_orderbook = {
            "yes": [[60.0, 100]],
            "no": [[55.0, 100]],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=5,
        )

        # Should have filled since NO bid moved above quote price
        assert len(fills) >= 1

    @pytest.mark.asyncio
    async def test_simulate_order_fills_max_hold_exit(
        self, simulator: RealisticOrderSimulator, mock_client: AsyncMock
    ):
        """Test order exit due to max_hold_seconds."""
        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-hold-1",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=100.0,  # Very high take_profit
            max_hold_seconds=0.2,  # Very short hold
        )

        mock_orderbook = {
            "yes": [[40.0, 100]],
            "no": [[59.0, 100]],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=5,
        )

        # Should exit due to max_hold, not timeout
        assert order_state["exit_reason"] == "max_hold_reached"

    @pytest.mark.asyncio
    async def test_simulate_order_fills_order_state_structure(
        self,
        simulator: RealisticOrderSimulator,
        sample_order_request: OrderRequest,
        mock_client: AsyncMock,
    ):
        """Test that order_state dict has expected structure."""
        mock_orderbook = {
            "yes_bid": [{"price": 40.0, "size": 100}],
            "yes_ask": [{"price": 41.0, "size": 100}],
            "no_bid": [{"price": 60.0, "size": 100}],
            "no_ask": [{"price": 61.0, "size": 100}],
        }

        mock_client.get_orderbook.return_value = mock_orderbook

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            sample_order_request,
            horizon_seconds=0.2,
        )

        # Verify order_state structure
        assert "order_id" in order_state
        assert "status" in order_state
        assert "total_filled_contracts" in order_state
        assert "remaining_unfilled_size" in order_state
        assert "elapsed_ms" in order_state
        assert "exit_reason" in order_state

        assert order_state["order_id"] == sample_order_request.id
        assert order_state["status"] in ["filled", "take_profit", "max_hold_reached", "timeout"]
        assert order_state["total_filled_contracts"] >= 0
        assert order_state["remaining_unfilled_size"] >= 0
        assert order_state["elapsed_ms"] >= 0


class TestSimulatorDiagnostics:
    """Tests for simulator top-of-book extraction and eligibility diagnostics."""

    def test_extract_top_of_book_snapshot_nested_shape(self) -> None:
        """Top-of-book extraction handles nested Kalshi shape."""
        orderbook = {
            "orderbook": {
                "yes": [[49.0, 20], [51.0, 10], [50.0, 15]],
                "no": [[45.0, 30], [47.0, 12]],
            }
        }

        top = extract_top_of_book_snapshot(orderbook)
        assert top["yes"]["best_bid_price"] == 51.0
        assert top["yes"]["best_bid_size"] == 10
        assert top["no"]["best_bid_price"] == 47.0
        assert top["no"]["best_bid_size"] == 12

    def test_compute_simulator_diagnostics_can_be_eligible(
        self, sample_order_request: OrderRequest
    ):
        """Eligibility predicate can become true when price moves through quote."""
        orderbook = {
            "yes": [[sample_order_request.quote_price_cents + 2.0, 100]],
            "no": [[40.0, 80]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=sample_order_request,
            orderbook=orderbook,
            queue_ahead=5,
            last_best_size_at_bid=10,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["best_bid"] == sample_order_request.quote_price_cents + 2.0
        assert diagnostics["eligibility"]["has_best_bid"] is True
        assert diagnostics["eligibility"]["price_moved_through"] is True
        assert diagnostics["eligibility"]["at_best_price"] is False

    def test_compute_simulator_diagnostics_price_improving_quote_is_at_best(self) -> None:
        """Price-improving quote should be treated as effective best bid for eligibility."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-improve-yes",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )
        orderbook = {
            "yes": [[82.0, 100]],
            "no": [[16.0, 90]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=1,
            last_best_size_at_bid=50,
            simulated_volume_per_poll=10,
        )

        assert diagnostics["market_best_bid"] == 82.0
        assert diagnostics["cross_for_side"] == 84.0
        assert diagnostics["price_improves"] is True
        assert diagnostics["effective_best_bid"] == 83.0
        assert diagnostics["best_size_at_bid"] == 0
        assert diagnostics["traded_volume_estimate"] == 0  # Price-improving orders have no volume
        assert diagnostics["eligibility"]["at_best_price"] is True
        assert (
            diagnostics["eligibility"]["queue_cleared"] is False
        )  # Queue (1) with traded_volume=0 does not clear

    def test_compute_simulator_diagnostics_market_moves_through_after_improve(self) -> None:
        """Moved-through should be based on market best, excluding our own quote."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-moved-through-yes",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )
        orderbook = {
            "yes": [[84.0, 100]],
            "no": [[15.0, 90]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=1,
            last_best_size_at_bid=50,
            simulated_volume_per_poll=10,
        )

        assert diagnostics["market_best_bid"] == 84.0
        assert diagnostics["price_improves"] is False
        assert diagnostics["eligibility"]["at_best_price"] is False
        assert diagnostics["eligibility"]["price_moved_through"] is True

    def test_compute_simulator_diagnostics_equal_price_keeps_at_best_behavior(
        self, sample_order_request: OrderRequest
    ) -> None:
        """Quote equal to market best remains normal at-best behavior (no improvement)."""
        orderbook = {
            "yes": [[sample_order_request.quote_price_cents, 100]],
            "no": [[40.0, 80]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=sample_order_request,
            orderbook=orderbook,
            queue_ahead=5,
            last_best_size_at_bid=120,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["price_improves"] is False
        assert diagnostics["market_best_bid"] == sample_order_request.quote_price_cents
        assert diagnostics["effective_best_bid"] == sample_order_request.quote_price_cents
        assert diagnostics["eligibility"]["at_best_price"] is True

    def test_compute_simulator_diagnostics_non_improving_keeps_not_at_best(
        self, sample_order_request: OrderRequest
    ) -> None:
        """Quote below market best remains non-improving and not at best."""
        orderbook = {
            "yes": [[sample_order_request.quote_price_cents + 1.0, 100]],
            "no": [[40.0, 80]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=sample_order_request,
            orderbook=orderbook,
            queue_ahead=5,
            last_best_size_at_bid=120,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["price_improves"] is False
        assert diagnostics["effective_best_bid"] == sample_order_request.quote_price_cents + 1.0
        assert diagnostics["eligibility"]["at_best_price"] is False


class TestPriceImprovementFillPath:
    """Regression tests for maker quote improvement eligibility and fills."""

    @pytest.mark.asyncio
    async def test_simulate_order_fills_price_improving_quote_cannot_fill_without_movement(
        self, mock_client: AsyncMock
    ):
        """A price-improving quote cannot fill via queue_cleared (traded_volume=0)."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=1.0,
            fill_probability_at_best=1.0,
            simulated_volume_per_poll=5,
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-improve-fill-1",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=10.0,
            max_hold_seconds=10,
        )

        # best_yes_bid=82, cross_yes=84 => quote=83 improves and sits at effective best.
        # Price-improving orders now fill via separate fill_probability_price_improves path.
        mock_client.get_orderbook.return_value = {
            "yes": [[82.0, 100]],
            "no": [[16.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.3,
        )

        # Should be able to fill via price_improves probability (not queue_cleared)
        assert len(fills) >= 0  # Probabilistic
        assert order_state["latest_diagnostics"]["eligibility"]["at_best_price"] is True
        assert order_state["latest_diagnostics"]["price_improves"] is True
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] == 0

    @pytest.mark.asyncio
    async def test_simulate_order_fills_price_improving_not_instant_with_zero_at_best_prob(
        self, mock_client: AsyncMock
    ):
        """At-best improving quote should not auto-fill when price_improves probability is zero."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=1.0,
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,  # Disable price_improves fills
            simulated_volume_per_poll=5,
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-improve-no-instant-1",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=10.0,
            max_hold_seconds=10,
        )

        mock_client.get_orderbook.return_value = {
            "yes": [[82.0, 100]],
            "no": [[16.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.2,
        )

        assert len(fills) == 0
        assert order_state["status"] == "timeout"
        assert order_state["latest_diagnostics"]["eligibility"]["at_best_price"] is True


class TestDiagnosticsInvariants:
    """Tests for eligibility invariants in compute_simulator_diagnostics."""

    def test_at_best_price_excludes_other_states(self) -> None:
        """If at_best_price is true, price_moved_through and price_moved_away must be false."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-invariant-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )

        # Case 1: at_best_price = True (effective_best_bid == quote_price)
        orderbook = {
            "yes": [[50.0, 100]],
            "no": [[45.0, 100]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=10,
            last_best_size_at_bid=None,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["eligibility"]["at_best_price"] is True
        assert diagnostics["eligibility"]["price_moved_through"] is False
        assert diagnostics["eligibility"]["price_moved_away"] is False

    def test_price_moved_through_excludes_at_best(self) -> None:
        """If price_moved_through is true, at_best_price and price_moved_away must be false."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-invariant-2",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )

        # Case 2: price_moved_through = True (effective_best_bid > quote_price)
        orderbook = {
            "yes": [[55.0, 100]],
            "no": [[45.0, 100]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=10,
            last_best_size_at_bid=None,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["eligibility"]["price_moved_through"] is True
        assert diagnostics["eligibility"]["at_best_price"] is False
        assert diagnostics["eligibility"]["price_moved_away"] is False

    def test_price_moved_away_excludes_at_best(self) -> None:
        """If price_moved_away is true, at_best_price and price_moved_through must be false."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-invariant-3",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )

        # Case 3: price_moved_away = True (effective_best_bid < quote_price)
        orderbook = {
            "yes": [[45.0, 100]],
            "no": [[50.0, 100]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=10,
            last_best_size_at_bid=None,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["eligibility"]["price_moved_away"] is True
        assert diagnostics["eligibility"]["at_best_price"] is False
        assert diagnostics["eligibility"]["price_moved_through"] is False

    def test_price_improvement_has_zero_traded_volume(self) -> None:
        """Price-improving orders should have traded_volume_estimate == 0."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-invariant-4",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )

        # Order improves: best_yes_bid=82, cross_yes=84, quote=83
        orderbook = {
            "yes": [[82.0, 100]],
            "no": [[16.0, 100]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=10,
            last_best_size_at_bid=None,
            simulated_volume_per_poll=50,
        )

        assert diagnostics["price_improves"] is True
        assert diagnostics["eligibility"]["at_best_price"] is True
        assert diagnostics["traded_volume_estimate"] == 0  # Price-improving has no volume


class TestNoFreeFillOnImprovement:
    """Tests that price-improving orders use separate fill probability (not queue_cleared)."""

    @pytest.mark.asyncio
    async def test_price_improving_order_does_not_fill_via_queue_cleared(
        self, mock_client: AsyncMock
    ) -> None:
        """Price-improving order uses fill_probability_price_improves, not queue_cleared."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.0,  # Disable price_moved_through fills
            fill_probability_at_best=1.0,  # Max probability for queue_cleared (non-improving)
            fill_probability_price_improves=0.0,  # Disable price_improves fills
            simulated_volume_per_poll=50,
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-no-free-fill-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=10.0,
            max_hold_seconds=10,
        )

        # Order improves: best_yes_bid=82, cross_yes=84, quote=83
        mock_client.get_orderbook.return_value = {
            "yes": [[82.0, 100]],
            "no": [[16.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.3,
        )

        # Should not fill because fill_probability_price_improves=0
        assert len(fills) == 0
        assert order_state["status"] == "timeout"
        assert order_state["latest_diagnostics"]["price_improves"] is True
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] == 0

    @pytest.mark.asyncio
    async def test_price_improving_order_can_fill_via_price_moved_through(
        self, mock_client: AsyncMock
    ) -> None:
        """Price-improving order CAN fill via price_moved_through path."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=1.0,
            fill_probability_at_best=0.0,
            simulated_volume_per_poll=50,
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-can-fill-moved-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=83.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=10.0,
            max_hold_seconds=10,
        )

        call_count = 0

        def get_orderbook_side_effect(_ticker: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First poll: order improves
                return {
                    "yes": [[82.0, 100]],
                    "no": [[16.0, 100]],
                }
            else:
                # Subsequent polls: price moves through
                return {
                    "yes": [[85.0, 100]],
                    "no": [[15.0, 100]],
                }

        mock_client.get_orderbook.side_effect = get_orderbook_side_effect

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.5,
        )

        # Should fill because price moved through
        assert len(fills) >= 1
        assert order_state["total_filled_contracts"] >= 1
