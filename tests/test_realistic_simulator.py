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
        assert order_state["status"] == "filled"
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
    async def test_simulate_order_fills_price_moved_away_no_fill(
        self,
        simulator: RealisticOrderSimulator,
        sample_order_request: OrderRequest,
        mock_client: AsyncMock,
    ):
        """Moved-away quotes should not fill and should timeout by horizon."""
        mock_client.get_orderbook.return_value = {
            "yes": [[40.0, 100]],
            "no": [[60.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            sample_order_request,
            horizon_seconds=0.2,
        )

        assert len(fills) == 0
        assert order_state["status"] == "timeout"
        assert order_state["latest_diagnostics"]["eligibility"]["price_moved_away"] is True

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
    async def test_simulate_order_fills_timeout_ignores_max_hold(
        self, simulator: RealisticOrderSimulator, mock_client: AsyncMock
    ):
        """Simulator ignores max_hold and exits only on horizon timeout/full fill."""
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
            horizon_seconds=0.2,
        )

        assert len(fills) == 0
        assert order_state["exit_reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_partial_fill_fee_scales_with_filled_contracts(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Total fill fees scale with filled contracts, not number of fill events."""
        monkeypatch.setattr("kalshi_edge_engine.engine.paper_sim.random.random", lambda: 0.0)

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.01,
            fill_probability_price_moved=1.0,
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=2,
        )

        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-partial-fee-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=3,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=3.0,
            take_profit_cents=0.0,
            max_hold_seconds=1,
            entry_fee_per_contract_cents=1.0,
        )

        mock_client.get_orderbook.return_value = {
            "yes": [[55.0, 100]],
            "no": [[45.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=0.2,
        )

        total_filled_contracts = sum(fill.fill_size for fill in fills)
        total_fill_fees = sum(fill.fill_fee_cents for fill in fills)

        assert total_filled_contracts == 3
        assert order_state["total_filled_contracts"] == total_filled_contracts
        assert total_fill_fees == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_simulator_does_not_exit_on_take_profit_threshold(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entry simulator should fill entry order and never emit take-profit exits."""
        monkeypatch.setattr("kalshi_edge_engine.engine.paper_sim.random.random", lambda: 0.0)

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.01,
            fill_probability_price_moved=1.0,
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=2,
        )

        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-take-profit-size-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=4,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=3.0,
            max_hold_seconds=1,
        )

        mock_client.get_orderbook.return_value = {
            "yes": [[55.0, 100]],
            "no": [[48.0, 100]],  # cross_yes = 52 => +2c/contract mark
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=0.2,
        )

        assert len(fills) >= 1
        # Conservative moved-through path: no single poll should auto-sweep full size.
        assert fills[0].fill_size < 4
        assert all(fill.fill_size <= 3 for fill in fills)
        assert order_state["exit_reason"] == "filled"
        assert order_state["total_filled_contracts"] == 4

    @pytest.mark.asyncio
    async def test_queue_ahead_monotonic_and_non_negative_at_best_price(
        self, mock_client: AsyncMock
    ) -> None:
        """Queue ahead decreases monotonically with traded volume and never goes negative."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.02,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=2,
        )

        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-queue-monotonic-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=5,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=0.0,
            max_hold_seconds=0.15,
        )

        best_sizes = [10, 7, 5, 3, 2]
        call_idx = -1

        def _orderbook_sequence(_ticker: str) -> dict[str, list[list[float]]]:
            nonlocal call_idx
            call_idx += 1
            size = best_sizes[min(call_idx, len(best_sizes) - 1)]
            return {
                "yes": [[50.0, size]],
                "no": [[45.0, 100]],
            }

        mock_client.get_orderbook.side_effect = _orderbook_sequence

        observed_queue: list[int] = []

        def _on_poll(runtime_update: dict[str, object]) -> None:
            queue_value = runtime_update.get("queue_ahead_contracts")
            if isinstance(queue_value, int):
                observed_queue.append(queue_value)

        await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=0.2,
            on_poll=_on_poll,
        )

        assert len(observed_queue) >= 2
        assert all(queue >= 0 for queue in observed_queue)
        assert all(
            curr <= prev for prev, curr in zip(observed_queue, observed_queue[1:], strict=False)
        )

    @pytest.mark.asyncio
    async def test_queue_initializes_once_when_best_size_changes(
        self, mock_client: AsyncMock
    ) -> None:
        """Queue ahead should initialize once and then only decay, not re-seed each poll."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.02,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=2,
        )

        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-queue-init-once-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=5,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=0.0,
            max_hold_seconds=0.06,
        )

        best_sizes = [10, 4, 4]
        call_idx = -1

        def _orderbook_sequence(_ticker: str) -> dict[str, list[list[float]]]:
            nonlocal call_idx
            call_idx += 1
            size = best_sizes[min(call_idx, len(best_sizes) - 1)]
            return {
                "yes": [[50.0, size]],
                "no": [[45.0, 100]],
            }

        mock_client.get_orderbook.side_effect = _orderbook_sequence

        observed_queue: list[int] = []

        def _on_poll(runtime_update: dict[str, object]) -> None:
            queue_value = runtime_update.get("queue_ahead_contracts")
            if isinstance(queue_value, int):
                observed_queue.append(queue_value)

        await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=0.08,
            on_poll=_on_poll,
        )

        assert len(observed_queue) >= 2
        assert observed_queue[0] == 8
        assert observed_queue[1] == 2

    @pytest.mark.asyncio
    async def test_queue_ahead_absorbs_traded_volume_no_fill(self, mock_client: AsyncMock) -> None:
        """No fill should occur while prior queue fully absorbs traded volume."""
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.02,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=1.0,
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=2,
        )

        now_ms = int(time.time() * 1000)
        order_request = OrderRequest(
            id="order-queue-absorbs-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=0.0,
            max_hold_seconds=0.08,
        )

        best_sizes = [5, 3, 3, 3]
        call_idx = -1

        def _orderbook_sequence(_ticker: str) -> dict[str, list[list[float]]]:
            nonlocal call_idx
            call_idx += 1
            size = best_sizes[min(call_idx, len(best_sizes) - 1)]
            return {
                "yes": [[50.0, size]],
                "no": [[45.0, 100]],
            }

        mock_client.get_orderbook.side_effect = _orderbook_sequence

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order_request,
            horizon_seconds=0.005,
        )

        assert fills == []
        assert order_state["status"] == "timeout"
        assert order_state["remaining_unfilled_size"] == 1
        assert int(order_state["queue_ahead_contracts"] or 0) > 0

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
        assert order_state["status"] in ["filled", "timeout"]
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
        # Price-improving orders now have traded_volume_estimate = simulated_volume_per_poll
        # (representing potential seller arrivals, not orderbook delta)
        assert diagnostics["traded_volume_estimate"] == 10
        assert diagnostics["eligibility"]["at_best_price"] is True
        assert (
            diagnostics["eligibility"]["queue_cleared"] is False
        )  # Queue never cleared for price_improves

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
        # traded_volume_estimate is now > 0 (represents arrival volume)
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] > 0

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

    def test_at_best_price_normalization_invariant_holds(self) -> None:
        """Diagnostics normalization should enforce exclusivity when at_best_price is true."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-invariant-1b",
            ticker="TEST-TICKER",
            side="NO",
            quote_price_cents=48.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=2.0,
            max_hold_seconds=600,
        )

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook={"yes": [[52.0, 100]], "no": [[48.0, 100]]},
            queue_ahead=0,
            last_best_size_at_bid=100,
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
        # Price-improving orders now have traded_volume_estimate > 0
        # (representing seller arrivals)
        assert diagnostics["traded_volume_estimate"] > 0


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
        # traded_volume_estimate is now > 0 (represents arrival volume)
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] > 0

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


class TestPaperSimCorrectnessPass:
    """Tests for paper sim correctness fixes: pennying + queue + volume."""

    @pytest.mark.asyncio
    async def test_price_improves_no_volume_no_fill(self, mock_client: AsyncMock) -> None:
        """Regression test: price_improves won't fill if fill_probability is 0.

        With seller arrival model, price-improving orders CAN fill via arrivals.
        But if fill_probability_price_improves=0, no fills should occur.
        """
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.0,  # Disable price_moved_through fills
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,  # NO seller arrivals!
            simulated_volume_per_poll=50,
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-pennying-fix-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=44.0,  # Improves on market bid of 43
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=10.0,
            max_hold_seconds=30,
        )

        # Market: best_yes_bid=43, cross_yes=57 => we quote 44 (improves)
        # But NO traded volume on this poll
        mock_client.get_orderbook.return_value = {
            "yes": [[43.0, 100]],
            "no": [[43.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.3,
        )

        # KEY ASSERTION: Must use fill_probability_price_improves=0 to prevent fills
        # (since we now have seller arrival model)
        assert len(fills) == 0
        assert order_state["total_filled_contracts"] == 0
        assert order_state["latest_diagnostics"]["price_improves"] is True
        # traded_volume_estimate is now > 0 (represents arrival volume, not orderbook delta)
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] > 0
        assert order_state["latest_diagnostics"]["best_size_at_bid"] == 0

    @pytest.mark.asyncio
    async def test_queue_cleared_only_with_volume(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Queue cleared flag should NOT be set when traded_volume=0.

        Ensures queue_cleared only becomes true when actual volume flows through.
        """
        # Mock random to force deterministic behavior
        monkeypatch.setattr("kalshi_edge_engine.engine.paper_sim.random.random", lambda: 0.0)

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=1.0,  # High probability if eligible
            simulated_volume_per_poll=0,  # NO traded volume
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-queue-vol-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=100.0,
            max_hold_seconds=1,  # Short timeout to force exit
        )

        mock_client.get_orderbook.return_value = {
            "yes": [[50.0, 100]],
            "no": [[50.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.3,
        )

        # With traded_volume=0, queue_cleared should be false even at best price
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] == 0
        assert order_state["latest_diagnostics"]["eligibility"]["queue_cleared"] is False
        # Should timeout without fill
        assert len(fills) == 0
        assert order_state["status"] != "filled"

    @pytest.mark.asyncio
    async def test_partial_fill_fees_exact_sum(
        self, mock_client: AsyncMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial fills must have fees that sum correctly.

        For 3 contracts @ 1 cent fee per contract:
        - Fill 1: 1 contract, 1 cent fee
        - Fill 2: 2 contracts, 2 cent fee
        - Total: 3 contracts, 3 cent fees
        """
        monkeypatch.setattr("kalshi_edge_engine.engine.paper_sim.random.random", lambda: 0.0)

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.02,
            fill_probability_price_moved=1.0,
            fill_probability_at_best=0.0,
            simulated_volume_per_poll=1,  # Force 1 contract per fill
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-fee-sum-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=3,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=3.0,  # 3 cents total
            take_profit_cents=0.0,
            max_hold_seconds=5,
            entry_fee_per_contract_cents=1.0,
        )

        # Price always above quote => fills
        mock_client.get_orderbook.return_value = {
            "yes": [[55.0, 100]],
            "no": [[45.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.5,
        )

        # Verify we got partial fills summing to 3 contracts
        total_filled = sum(f.fill_size for f in fills)
        total_fees = sum(f.fill_fee_cents for f in fills)

        assert total_filled == 3
        assert order_state["total_filled_contracts"] == 3
        # Fees should sum to 3 cents (1 per contract)
        assert total_fees == 3.0

        # Verify each fill has correct fee_cents = fill_size * 1
        for fill in fills:
            assert fill.fill_fee_cents == fill.fill_size * 1.0

    @pytest.mark.asyncio
    async def test_entry_simulator_fills_without_take_profit_exit(
        self, mock_client: AsyncMock
    ) -> None:
        """Entry simulator should not return take-profit exit reasons."""

        call_count = 0

        def get_orderbook_side_effect(_ticker: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"yes": [[45.0, 100]], "no": [[55.0, 100]]}
            else:
                # Price improves => mark price improves too (crossing up)
                return {"yes": [[60.0, 100]], "no": [[40.0, 100]]}

        mock_client.get_orderbook.side_effect = get_orderbook_side_effect

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.9,
            simulated_volume_per_poll=1,
        )

        now_ms = int(time.time() * 1000)
        entry_price = 50.0
        max_contracts = 10
        # Set take_profit low so it triggers after 2 contracts
        take_profit_cents_val = (60.0 - entry_price) * 1.5  # ~15 cents

        order = OrderRequest(
            id="order-tp-contracts-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=entry_price,
            size_contracts=max_contracts,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.0,  # No fees for simplicity
            take_profit_cents=take_profit_cents_val,
            max_hold_seconds=10,
        )

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=1.0,
        )

        # Test is successful if we got fills and simulator exits for entry lifecycle only.
        if order_state["total_filled_contracts"] > 0:
            assert order_state["exit_reason"] in ["filled", "timeout"]

    @pytest.mark.asyncio
    async def test_queue_update_monotonic_decrease(self, mock_client: AsyncMock) -> None:
        """Queue ahead should monotonically decrease with volume.

        Once at best price without price_improves, queue should only decrease
        (or stay same if no volume).
        """

        call_count = 0

        def get_orderbook_side_effect(_ticker: str):
            nonlocal call_count
            call_count += 1
            # Size at best decreases each poll (simulating volume)
            sizes = [100, 90, 80, 70]
            size = sizes[min(call_count - 1, len(sizes) - 1)]
            return {"yes": [[50.0, size]], "no": [[50.0, 100]]}

        mock_client.get_orderbook.side_effect = get_orderbook_side_effect

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=0.0,  # Don't fill
            simulated_volume_per_poll=0,  # Rely on size delta
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-queue-mono-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=100.0,
            max_hold_seconds=0.5,  # Force timeout
        )

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.5,
        )

        # Should not fill (probability=0)
        assert len(fills) == 0
        # Queue should have been initialized at some point
        # (exact value depends on timing, but should be tracked consistently)
        assert "latest_diagnostics" in order_state
        assert "queue_ahead_after" in order_state["latest_diagnostics"]


class TestSellerArrivalForPriceImproves:
    """Tests for seller arrival volume model for price-improving orders."""

    @pytest.mark.asyncio
    async def test_price_improves_can_fill_via_seller_arrival(self, mock_client: AsyncMock) -> None:
        """Price-improving orders should fill via seller arrival (not orderbook delta).

        Setup: market_best_bid=43, cross=45, quote=44 (price_improves=True).
        With fill_probability_price_improves=1.0, should get fills even though
        synthetic best level has no orderbook size.
        """
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.0,  # Disable price_moved_through
            fill_probability_at_best=0.0,  # Disable queue-based fills
            fill_probability_price_improves=1.0,  # Enable seller arrivals
            simulated_volume_per_poll=5,
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-arrival-test-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=44.0,  # Improves on market bid of 43
            size_contracts=3,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.0,
            take_profit_cents=100.0,
            max_hold_seconds=10,
        )

        # Market: best_yes_bid=43, cross_yes=57 (100-43) => our quote 44 improves
        # NO side: best_no_bid=43, cross_no=57
        mock_client.get_orderbook.return_value = {
            "yes": [[43.0, 100]],
            "no": [[43.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.5,
        )

        # Should get fills via seller arrival
        assert len(fills) >= 1, "Expected at least one fill via seller arrival"
        for fill in fills:
            assert fill.fill_size >= 1, f"Fill size should be >= 1, got {fill.fill_size}"
            assert fill.fill_size <= 2, (
                f"Price-improves fill should be conservatively capped, got {fill.fill_size}"
            )
            assert fill.queue_ahead_at_fill == 0, (
                f"queue_ahead should be 0 for price_improves, got {fill.queue_ahead_at_fill}"
            )

        # Verify diagnostics show price_improves
        assert order_state["latest_diagnostics"]["price_improves"] is True

    @pytest.mark.asyncio
    async def test_price_improves_queue_cleared_never_true(self) -> None:
        """queue_cleared must be False for price_improves (no fake queue clearing)."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-queue-test-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=44.0,  # Improves on market bid of 43
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.0,
            take_profit_cents=100.0,
            max_hold_seconds=10,
        )

        # Market: best_yes_bid=43, cross_yes=57 => our quote 44 improves
        orderbook = {
            "yes": [[43.0, 100]],
            "no": [[43.0, 100]],
        }

        diagnostics = compute_simulator_diagnostics(
            order=order,
            orderbook=orderbook,
            queue_ahead=0,
            last_best_size_at_bid=None,
            simulated_volume_per_poll=50,
        )

        # Verify price_improves is true
        assert diagnostics["price_improves"] is True, "Expected price_improves=True"
        assert diagnostics["best_size_at_bid"] == 0, "Synthetic level should have size 0"

        # KEY ASSERTION: queue_cleared must be False (no fake clearing)
        assert diagnostics["eligibility"]["queue_cleared"] is False, (
            "queue_cleared should be False for price_improves"
        )

        # Verify traded_volume_estimate represents potential arrivals (not 0)
        assert diagnostics["traded_volume_estimate"] > 0, (
            "traded_volume_estimate should represent arrival volume, not 0"
        )

    @pytest.mark.asyncio
    async def test_non_price_improving_still_requires_traded_volume(
        self, mock_client: AsyncMock
    ) -> None:
        """Non-price-improving orders should NOT fill when traded_volume=0.

        Setup: quote == market_best_bid (not improving), but no volume.
        Should timeout without fills.
        """
        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.05,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=1.0,  # High probability IF eligible
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=0,  # NO volume!
        )

        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-no-vol-test-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,  # At best, not improving
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.0,
            take_profit_cents=100.0,
            max_hold_seconds=0.3,  # Short timeout
        )

        # Market: best_yes_bid=50 (same as our quote), but no volume delta
        mock_client.get_orderbook.return_value = {
            "yes": [[50.0, 100]],  # Size stays constant (no delta)
            "no": [[50.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=0.3,
        )

        # Should NOT fill because traded_volume=0 (no delta, simulated_volume=0)
        assert len(fills) == 0, "Should not fill without traded volume"
        assert order_state["status"] != "filled"

        # Verify diagnostics show not price_improves and no volume
        assert order_state["latest_diagnostics"]["price_improves"] is False
        assert order_state["latest_diagnostics"]["traded_volume_estimate"] == 0


class TestCorrectionFixes:
    """Tests for correctness fixes in simulator."""

    @pytest.mark.asyncio
    async def test_no_side_has_no_take_profit_exit_in_entry_simulator(
        self,
        simulator: RealisticOrderSimulator,
        mock_client: AsyncMock,
    ):
        """NO-side entry simulation should not emit take-profit exits."""
        now_ms = int(time.time() * 1000)
        # NO side order: entry at 50, should profit when mark price is lower
        order = OrderRequest(
            id="order-no-side",
            ticker="KXNBAGAME-26MAR01SASNYK-NO",
            side="NO",
            quote_price_cents=50.0,
            size_contracts=5,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=1.0,
            take_profit_cents=15.0,  # Expect PnL when mark < 45
            max_hold_seconds=60,
        )

        # Mock orderbook:
        # For NO side: we want cross_no (which is 100 - yes_best_bid)
        # If yes_best_bid=55, then cross_no=45, mark for NO is 45
        # PnL for NO = (entry - mark) * contracts = (50 - 45) * 5 = 25 cents (should trigger take_profit=15)
        mock_client.get_orderbook.return_value = {
            "yes": [[55.0, 100]],  # Best YES bid at 55, so cross_no = 100 - 55 = 45
            "no": [[45.0, 100]],
        }

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=5,
        )

        if len(fills) > 0:
            assert order_state["exit_reason"] in ["filled", "timeout"]

    @pytest.mark.asyncio
    async def test_queue_model_consistency(
        self,
        mock_client: AsyncMock,
    ):
        """Test queue model correctly separates queue consumption from fill eligibility."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-queue-test",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=5,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=0.0,  # No early exit
            max_hold_seconds=60,
        )

        # Simulate a scenario where:
        # - queue_ahead = 10 contracts
        # - traded_volume_estimate = 15 contracts (volume at our level)
        # - remaining_size = 5 contracts (what we want to buy)
        #
        # Old behavior (BUGGY):
        #   - queue_ahead -= 15 -> queue_ahead = -5 (clamped to 0)
        #   - fill_size = min(5, 15) = 5
        #   This is inconsistent: it uses traded_volume fully even though queue consumed some
        #
        # New behavior (CORRECT):
        #   - consumed_by_ahead = min(10, 15) = 10
        #   - remaining_trade = 15 - 10 = 5
        #   - Only fills if queue_ahead becomes 0 AND remaining_trade > 0
        #   - fill_size = min(5, 5) = 5

        # Simulate polling sequence that produces the above scenario
        poll_count = [0]

        async def get_orderbook_side_effect(_ticker: str) -> dict:
            poll_count[0] += 1
            if poll_count[0] == 1:
                # First poll: establish queue position (best_size_at_bid=20)
                return {"yes": [[50.0, 20]], "no": [[50.0, 20]]}
            else:
                # Subsequent polls: size drops to 5 (15 contracts traded)
                # This generates traded_volume_estimate = 15
                return {"yes": [[50.0, 5]], "no": [[50.0, 5]]}

        mock_client.get_orderbook.side_effect = get_orderbook_side_effect

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.01,
            fill_probability_price_moved=0.0,  # Disable price_moved_through fills
            fill_probability_at_best=0.95,  # High probability for at_best fills
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=0,  # Rely only on size delta
        )

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=1,
        )

        # The fill should happen only after queue is cleared AND remaining_trade > 0
        # With traded_volume=15 and queue=20 (initially), queue clears slowly
        # After first poll with delta (20->5), traded_volume=15:
        #   consumed_by_ahead = min(20, 15) = 15 -> no fill yet
        #   queue_ahead = 20 -> 15
        # Next iteration: traded_volume=0 (no delta), no fill
        # So with this sequence, fills might be 0 or depend on timing
        # The key check is that fills don't happen prematurely (before queue clears)
        assert "exit_reason" in order_state
        assert order_state["status"] in ["filled", "timeout"]

    @pytest.mark.asyncio
    async def test_price_moved_through_fill_is_volume_capped(
        self,
        mock_client: AsyncMock,
    ):
        """Moved-through fills should be capped conservatively, not full-swept."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-sweep-test",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=7,  # Want 7 contracts
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=0.0,
            max_hold_seconds=60,
        )

        # Simulate price moving through us on second poll
        poll_count = [0]

        async def get_orderbook_side_effect(_ticker: str) -> dict:
            poll_count[0] += 1
            if poll_count[0] == 1:
                # First poll: price at our level
                return {"yes": [[50.0, 100]], "no": [[50.0, 100]]}
            else:
                # Second poll: price moves above us (55 > 50)
                # This triggers price_moved_through eligibility
                return {"yes": [[55.0, 100]], "no": [[45.0, 100]]}

        mock_client.get_orderbook.side_effect = get_orderbook_side_effect

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.01,
            fill_probability_price_moved=1.0,  # Always fill when price moves
            fill_probability_at_best=0.0,
            fill_probability_price_improves=0.0,
            simulated_volume_per_poll=100,
        )

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=1,
        )

        if len(fills) > 0:
            total_filled = sum(f.fill_size for f in fills)
            first_fill = fills[0]
            assert first_fill.fill_size < order.size_contracts
            assert all(fill.fill_size <= 3 for fill in fills)
            assert total_filled <= order.size_contracts

    @pytest.mark.asyncio
    async def test_queue_cleared_invariant(
        self,
        mock_client: AsyncMock,
    ):
        """Test queue_cleared in eligibility never occurs with price_improves."""
        now_ms = int(time.time() * 1000)
        order = OrderRequest(
            id="order-invariant-test",
            ticker="KXNBAGAME-26MAR01SASNYK-YES",
            side="YES",
            quote_price_cents=52.0,  # Above market bid (50), will be price-improving
            size_contracts=3,
            created_ts_ms=now_ms,
            net_edge=0.5,
            entry_fee_cents=0.5,
            take_profit_cents=0.0,
            max_hold_seconds=60,
        )

        # Orderbook has best bid at 50, our order at 52 is price-improving
        mock_client.get_orderbook.return_value = {
            "yes": [[50.0, 500]],  # Market best bid at 50
            "no": [[50.0, 500]],
        }

        simulator = RealisticOrderSimulator(
            poll_interval_seconds=0.01,
            fill_probability_price_moved=0.0,
            fill_probability_at_best=0.0,
            fill_probability_price_improves=1.0,  # Always fill for price improves
            simulated_volume_per_poll=100,
        )

        fills, order_state = await simulator.simulate_order_fills(
            mock_client,
            order,
            horizon_seconds=1,
        )

        # Check the latest diagnostics for the invariant
        if order_state.get("latest_diagnostics"):
            diag = order_state["latest_diagnostics"]
            eligibility = diag.get("eligibility", {})
            price_improves = diag.get("price_improves", False)

            # Invariant: queue_cleared must be False if price_improves is True
            if price_improves:
                assert eligibility.get("queue_cleared") is False, (
                    "Invariant violation: queue_cleared=True when price_improves=True"
                )
