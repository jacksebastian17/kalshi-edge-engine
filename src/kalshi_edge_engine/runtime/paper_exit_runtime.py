"""Paper-only position liquidation and timed post-fill observations."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kalshi_edge_engine.db.paper_store import MARK_MAX_AGE_MS, OUTCOME_MAX_DELAY_MS, PaperStore
from kalshi_edge_engine.engine.fees import LEGACY_FEE_PROFILE, FeeProfile, estimate_taker_fee_cents
from kalshi_edge_engine.engine.orderbook import LiquidationQuote, quote_liquidation
from kalshi_edge_engine.engine.position_exit import decide_position_exit, should_take_profit
from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.kalshi.market import read_settlement
from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import state as runtime_state

logger = logging.getLogger(__name__)
SETTLEMENT_POLL_SECONDS = 15


async def _position_fee_profiles(tickers: set[str]) -> dict[str, FeeProfile | None]:
    resolver = runtime_state.market_fees
    if resolver is None:
        return dict.fromkeys(tickers)
    limit = asyncio.Semaphore(5)

    async def resolve(ticker: str) -> FeeProfile | None:
        async with limit:
            return await resolver.resolve(ticker)

    ordered = sorted(tickers)
    profiles = await asyncio.gather(*(resolve(ticker) for ticker in ordered))
    return dict(zip(ordered, profiles, strict=True))


async def _reconcile_markets(
    store: PaperStore,
    client: KalshiClient,
    tickers: set[str],
    checks: dict[str, tuple[float, dict[str, Any] | None]],
) -> set[str]:
    """Poll held markets independently of depth and keep known nontrading markets off the bid path."""
    limit = asyncio.Semaphore(5)

    async def refresh(ticker: str) -> None:
        deadline, previous = checks.get(ticker, (0, None))
        if time.monotonic() < deadline:
            return
        market: dict[str, Any] | None = previous
        async with limit:
            try:
                fetched = await client.get_market(ticker)
                if isinstance(fetched, dict) and fetched.get("ticker") == ticker:
                    market = fetched
            except Exception:
                logger.debug(
                    "paper_market_status_unavailable", extra={"ticker": ticker}, exc_info=True
                )
        checks[ticker] = (time.monotonic() + SETTLEMENT_POLL_SECONDS, market)

    for stale_ticker in checks.keys() - tickers:
        del checks[stale_ticker]
    await asyncio.gather(*(refresh(ticker) for ticker in sorted(tickers)))
    blocked: set[str] = set()
    for ticker, (_, market) in checks.items():
        if not market:
            continue
        if market.get("status") in (
            "initialized",
            "inactive",
            "closed",
            "determined",
            "disputed",
            "amended",
            "finalized",
            "settled",
        ):
            blocked.add(ticker)
        settlement = read_settlement(market)
        if settlement is not None and store.reconcile_market_settlement(
            settlement, now_ms=int(time.time() * 1000)
        ):
            logger.info(
                "paper_market_settled", extra={"ticker": ticker, "result": settlement.result}
            )
    return blocked


async def _fetch_books(
    client: KalshiClient, tickers: set[str]
) -> dict[str, tuple[dict[str, Any], int]]:
    """Fetch each needed ticker once, with a bounded number of concurrent GETs."""
    semaphore = asyncio.Semaphore(5)

    async def fetch(ticker: str) -> tuple[str, dict[str, Any], int] | None:
        async with semaphore:
            try:
                async with asyncio.timeout(MARK_MAX_AGE_MS / 1000):
                    started = time.monotonic()
                    orderbook = await client.get_orderbook(ticker)
                    if (time.monotonic() - started) * 1000 > MARK_MAX_AGE_MS:
                        return None
                    return ticker, orderbook, int(time.time() * 1000)
            except Exception:
                logger.debug("paper_orderbook_unavailable", extra={"ticker": ticker}, exc_info=True)
                return None

    results = await asyncio.gather(*(fetch(ticker) for ticker in sorted(tickers)))
    return {row[0]: (row[1], row[2]) for row in results if row is not None}


async def _observe_due_outcomes(store: PaperStore, client: KalshiClient, now_ms: int) -> None:
    pending = [
        (horizon, row)
        for horizon in (5, 15, 30, 60)
        for row in store.get_pending_fill_outcome_updates(
            horizon_seconds=horizon,
            cutoff_ts_ms=now_ms - horizon * 1000,
            earliest_ts_ms=now_ms - horizon * 1000 - OUTCOME_MAX_DELAY_MS,
        )
    ]
    tickers = {row["ticker"] for _, row in pending}
    profiles = await _position_fee_profiles(tickers)
    books = await _fetch_books(client, tickers)
    for horizon, row in pending:
        if row["ticker"] not in books:
            continue
        book, observed_ts_ms = books[row["ticker"]]
        profile = profiles[row["ticker"]]
        if profile is None and row["experiment_id"]:
            continue
        quote = quote_liquidation(
            book,
            row["side"],
            row["fill_size"],
            observed_ts_ms,
            fee_profile=profile or LEGACY_FEE_PROFILE,
        )
        if quote is None:
            continue
        pnl = store.compute_short_horizon_fill_pnl_cents(
            side=row["side"],
            fill_price_cents=row["fill_price_cents"],
            fill_size=row["fill_size"],
            mark_price_cents=quote.price_cents,
            fill_fee_cents=row["fill_fee_cents"],
            exit_fee_cents=quote.exit_fee_cents,
        )
        store.update_fill_outcome(
            fill_id=row["fill_id"],
            horizon_seconds=horizon,
            mark_after_fill_cents=quote.price_cents,
            pnl_after_fill_cents=pnl,
            observed_ts_ms=observed_ts_ms,
            liquidation=quote,
        )


async def _refresh_positions(
    store: PaperStore,
    client: KalshiClient,
    *,
    market_checks: dict[str, tuple[float, dict[str, Any] | None]] | None = None,
) -> dict[tuple[str, str], LiquidationQuote]:
    positions = store.get_positions()
    tickers = {pos["ticker"] for pos in positions}
    blocked = await _reconcile_markets(
        store,
        client,
        tickers | store.get_open_intent_tickers(),
        market_checks if market_checks is not None else {},
    )
    profiles = await _position_fee_profiles(tickers - blocked)
    books = await _fetch_books(client, tickers - blocked)
    now_ms = int(time.time() * 1000)
    marks: dict[tuple[str, str], LiquidationQuote] = {}
    for pos in positions:
        ticker, side, size = pos["ticker"], pos["side"], pos["net_contracts"]
        if ticker not in books:
            continue
        book, observed_ts_ms = books[ticker]
        profile = profiles[ticker]
        if profile is None and pos["clean_lifecycle"]:
            continue
        quote = quote_liquidation(
            book, side, size, observed_ts_ms, fee_profile=profile or LEGACY_FEE_PROFILE
        )
        if quote is None or not quote.is_fresh(now_ms, MARK_MAX_AGE_MS):
            continue
        entry_fees = pos["open_entry_fees_cents"]
        if entry_fees is None:
            entry_fees = store.get_open_lifecycle_entry_fees_cents(
                ticker=ticker, side=side, first_open_ts_ms=pos["first_open_ts_ms"]
            )
        take_profit = pos["take_profit_cents"]
        max_hold = pos["max_hold_seconds"]
        decision = decide_position_exit(
            side=side,
            avg_entry_price_cents=pos["avg_entry_price_cents"],
            mark_price_cents=quote.price_cents,
            first_open_ts_ms=pos["first_open_ts_ms"],
            now_ms=now_ms,
            max_hold_seconds=(
                runtime_config.PAPER_MAX_HOLD_SECONDS if max_hold is None else max_hold
            ),
            take_profit_cents_per_contract=(
                runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
                if take_profit is None
                else take_profit
            ),
            liquidation_fees_cents_per_contract=(entry_fees + quote.exit_fee_cents) / size,
        )
        if decision.reason and store.close_position(
            pos, quote, now_ms=now_ms, reason=decision.reason
        ):
            logger.info(
                "paper_position_closed",
                extra={"ticker": ticker, "side": side, "reason": decision.reason},
            )
            continue
        marks[ticker, side] = quote
    return marks


async def _paper_maintenance_loop(client: KalshiClient) -> None:
    """Maintain exposure and outcomes even when discovery returns no markets."""

    async def run(worker: Callable[[KalshiClient], Awaitable[None]]) -> None:
        await worker(client)
        raise RuntimeError("Paper maintenance child task returned")

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run(_position_maintenance_loop))
        tasks.create_task(run(_outcome_maintenance_loop))


async def _outcome_maintenance_loop(client: KalshiClient) -> None:
    while True:
        store = runtime_state.paper_store
        if runtime_config.ENABLE_PAPER_TRADING and store is not None:
            try:
                await _observe_due_outcomes(store, client, int(time.time() * 1000))
            except Exception:
                logger.exception("paper_outcome_observation_error")
        await asyncio.sleep(1)


async def _position_maintenance_loop(client: KalshiClient) -> None:
    market_checks: dict[str, tuple[float, dict[str, Any] | None]] = {}
    while True:
        store = runtime_state.paper_store
        if runtime_config.ENABLE_PAPER_TRADING and store is not None:
            try:
                marks = await _refresh_positions(store, client, market_checks=market_checks)
                runtime_state.paper_liquidation_marks = marks
                now_ms = int(time.time() * 1000)
                risk = store.evaluate_daily_loss(
                    now_ms=now_ms,
                    max_loss_cents=runtime_config.PAPER_MAX_DAILY_LOSS_CENTS,
                    liquidation_marks=marks,
                )
                if (
                    now_ms - runtime_state.last_pnl_snapshot_ts_ms
                    >= runtime_config.PAPER_PNL_SNAPSHOT_INTERVAL_MS
                ):
                    metrics = store.compute_paper_metrics(liquidation_marks=marks, now_ms=now_ms)
                    detail_keys = (
                        "experiment_id",
                        "experiment_cutover_ts_ms",
                        "non_experiment_realized_pnl_cents",
                        "clean_open_positions_count",
                    )
                    store.insert_pnl_snapshot(
                        ts_ms=now_ms,
                        realized_pnl_cents=metrics["clean_realized_pnl_cents"],
                        unrealized_pnl_cents=metrics["clean_unrealized_pnl_cents"],
                        open_positions_count=metrics["open_positions_count"],
                        open_orders_count=metrics["open_orders_count"],
                        valuation_details={
                            **{key: metrics[key] for key in detail_keys},
                            "unrealized_pnl_status": metrics["clean_unrealized_status"],
                            "unrealized_unmarked_positions_count": metrics[
                                "clean_unmarked_positions_count"
                            ],
                            **risk,
                        },
                    )
                    runtime_state.last_pnl_snapshot_ts_ms = now_ms
            except Exception:
                runtime_state.paper_liquidation_marks = {}
                logger.exception("paper_maintenance_error")
        await asyncio.sleep(1)


def _close_position(
    *,
    ticker: str,
    side: str,
    net_contracts: int,
    avg_entry_price_cents: float,
    realized_pnl_cents: float,
    first_open_ts_ms: int,
    mark_price_cents: float | None,
    now_ms: int,
    reason: str,
) -> bool:
    """Compatibility helper for callers supplying an already priced simulated sale."""
    del realized_pnl_cents  # The persisted cumulative balance is authoritative.
    store = runtime_state.paper_store
    if store is None or net_contracts <= 0 or mark_price_cents is None:
        return False
    return store.close_position(
        {
            "ticker": ticker,
            "side": side,
            "avg_entry_price_cents": avg_entry_price_cents,
            "first_open_ts_ms": first_open_ts_ms,
        },
        LiquidationQuote(
            size_contracts=net_contracts,
            price_cents=mark_price_cents,
            exit_fee_cents=estimate_taker_fee_cents(mark_price_cents, net_contracts),
            best_bid_cents=mark_price_cents,
            implied_ask_cents=None,
            bid_levels=((mark_price_cents, net_contracts),),
            observed_ts_ms=now_ms,
        ),
        now_ms=now_ms,
        reason=reason,
        execution_source="simulated_caller_supplied_bid",
    )


def _maybe_close_position_take_profit(
    *,
    ticker: str,
    side: str,
    net_contracts: int,
    avg_entry_price_cents: float,
    realized_pnl_cents: float,
    first_open_ts_ms: int,
    mark_price_cents: float,
    now_ms: int,
) -> bool:
    """Run the compatibility helper with the same net take-profit decision."""
    store = runtime_state.paper_store
    if store is None or net_contracts <= 0:
        return False
    entry_fees = store.get_open_lifecycle_entry_fees_cents(
        ticker=ticker, side=side, first_open_ts_ms=first_open_ts_ms
    )
    exit_fees = estimate_taker_fee_cents(mark_price_cents, net_contracts)
    if not should_take_profit(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents - (entry_fees + exit_fees) / net_contracts,
        take_profit_cents_per_contract=runtime_config.PAPER_TAKE_PROFIT_CENTS,
    ):
        return False
    return _close_position(
        ticker=ticker,
        side=side,
        net_contracts=net_contracts,
        avg_entry_price_cents=avg_entry_price_cents,
        realized_pnl_cents=realized_pnl_cents,
        first_open_ts_ms=first_open_ts_ms,
        mark_price_cents=mark_price_cents,
        now_ms=now_ms,
        reason="take_profit",
    )
