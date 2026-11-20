"""NBA scanner background runtime loop."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from kalshi_edge_engine.engine.fees import LEGACY_FEE_PROFILE
from kalshi_edge_engine.engine.signal import Signal, score_signal
from kalshi_edge_engine.engine.universe import discover_nba_universe
from kalshi_edge_engine.kalshi.client import KalshiClient, KalshiReadBudgetError
from kalshi_edge_engine.kalshi.read_budget import ReadBudget
from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import state as runtime_state
from kalshi_edge_engine.runtime.paper_runtime import _process_paper_intents
from kalshi_edge_engine.universes.cbb import discover_cbb_universe

logger = logging.getLogger(__name__)


async def _discover_enabled_universe_markets(
    client: KalshiClient,
) -> tuple[list[str], dict[str, str], dict[str, int], list[str]]:
    """Discover and merge market tickers across enabled universes.

    Returns:
        merged_tickers: Deduplicated sorted list of tickers.
        ticker_universe_map: Ticker -> universe key (first discovered owner).
        discovered_universe_counts: universe key -> discovered market count.
        enabled_universes: normalized enabled universe list.
    """
    enabled_universes = runtime_config.get_enabled_universes()
    discovered_universe_counts: dict[str, int] = {}
    ticker_universe_map: dict[str, str] = {}
    merged_set: set[str] = set()

    if "NBA" in enabled_universes:
        nba_universe = await discover_nba_universe(client)
        discovered_universe_counts["NBA"] = len(nba_universe.market_tickers)
        for ticker in nba_universe.market_tickers:
            if ticker not in merged_set:
                ticker_universe_map[ticker] = "NBA"
            merged_set.add(ticker)

    if "CBB" in enabled_universes:
        cbb_universe = await discover_cbb_universe(client)
        discovered_universe_counts["CBB"] = len(cbb_universe.market_tickers)
        for ticker in cbb_universe.market_tickers:
            if ticker not in merged_set:
                ticker_universe_map[ticker] = "CBB"
            merged_set.add(ticker)

    return sorted(merged_set), ticker_universe_map, discovered_universe_counts, enabled_universes


async def _nba_scanner_loop(
    client: KalshiClient,
    *,
    signal_to_response: Callable[[Signal], dict[str, Any]],
    signal_response_model: type,
    top_signals_snapshot_model: type,
) -> None:
    """Background loop that scans enabled universes with predictable cadence.

    - Discovers enabled universes (NBA/CBB)
    - Each cycle, scans a rotating slice of N=100 tickers
    - Skips tickers in cooldown after failure (60s TTL)
    - Maintains predictable 2s target cycle time
    - Updates top 20 signals from slice results
    - Maintains scanner state for /scanner/status endpoint

    Args:
        client: KalshiClient instance.
    """
    TARGET_CYCLE_SECONDS = 2.0
    COOLDOWN_MS = 60_000
    scan_slice_size = 100
    universe_refresh_ttl_ms = 300_000

    logger.info("multi_universe_scanner_loop_started")
    runtime_state.scanner_state.enabled = True

    while True:
        cycle_start_ms = int(time.time() * 1000)

        try:
            now_ms = int(time.time() * 1000)
            expired_tickers = [
                t
                for t, until_ms in runtime_state.scanner_state.cooldown_until_ms.items()
                if now_ms >= until_ms
            ]
            for t in expired_tickers:
                del runtime_state.scanner_state.cooldown_until_ms[t]

            should_refresh_universe = (
                now_ms - runtime_state.scanner_state.last_universe_refresh_ms
                > universe_refresh_ttl_ms
            )

            if should_refresh_universe or not runtime_state.scanner_state.market_tickers:
                logger.info("multi_universe_scanner_refreshing_universe")
                (
                    merged_tickers,
                    ticker_universe_map,
                    discovered_universe_counts,
                    enabled_universes,
                ) = await _discover_enabled_universe_markets(client)
                runtime_state.scanner_state.market_tickers = merged_tickers
                runtime_state.scanner_state.ticker_universe_map = ticker_universe_map
                runtime_state.scanner_state.enabled_universes = enabled_universes
                runtime_state.scanner_state.discovered_universe_counts = discovered_universe_counts
                runtime_state.scanner_state.markets_discovered = len(merged_tickers)
                runtime_state.scanner_state.last_universe_refresh_ms = now_ms
                runtime_state.scanner_state.rotating_index = 0

            market_tickers = runtime_state.scanner_state.market_tickers

            if not market_tickers:
                logger.warning("multi_universe_scanner_no_markets")
                runtime_state.scanner_state.last_error = "No markets in universe"
                cycle_end_ms = int(time.time() * 1000)
                cycle_duration_ms = cycle_end_ms - cycle_start_ms
                runtime_state.scanner_state.last_cycle_duration_ms = cycle_duration_ms
                sleep_ms = max(0, (TARGET_CYCLE_SECONDS * 1000) - cycle_duration_ms)
                await asyncio.sleep(sleep_ms / 1000.0)
                continue

            runtime_state.scanner_state.tickers_failed_last_cycle = []
            runtime_state.scanner_state.tickers_skipped_cooldown_last_cycle = []

            start_idx = runtime_state.scanner_state.rotating_index
            available_tickers: list[str] = []
            skipped_count = 0

            budget = getattr(client, "read_budget", None)
            cycle_slice = (
                budget.discovery_slice_size(TARGET_CYCLE_SECONDS, scan_slice_size)
                if isinstance(budget, ReadBudget)
                else scan_slice_size
            )
            idx = start_idx
            while len(available_tickers) < cycle_slice and len(
                available_tickers
            ) + skipped_count < len(market_tickers):
                ticker = market_tickers[idx % len(market_tickers)]
                if ticker not in runtime_state.scanner_state.cooldown_until_ms:
                    available_tickers.append(ticker)
                else:
                    skipped_count += 1
                    runtime_state.scanner_state.tickers_skipped_cooldown_last_cycle.append(ticker)
                idx += 1

            runtime_state.scanner_state.rotating_index = idx % len(market_tickers)
            slice_tickers = available_tickers

            semaphore = asyncio.Semaphore(5)
            all_signals: list[Signal] = []

            async def fetch_and_score(
                ticker: str, sem: asyncio.Semaphore, api_client: KalshiClient
            ) -> tuple[str, Signal | None, bool]:
                """Fetch a signal and distinguish budget pressure from ticker-specific errors."""
                async with sem:
                    try:
                        resolver = runtime_state.market_fees
                        fee_profile = (
                            await resolver.resolve(ticker, for_entry=True)
                            if resolver is not None
                            else LEGACY_FEE_PROFILE
                        )
                        orderbook = await api_client.get_orderbook(ticker)
                        universe_key = runtime_state.scanner_state.ticker_universe_map.get(
                            ticker,
                            "UNKNOWN",
                        )
                        return (
                            ticker,
                            score_signal(
                                ticker, orderbook, universe=universe_key, fee_profile=fee_profile
                            ),
                            False,
                        )
                    except KalshiReadBudgetError:
                        return ticker, None, True
                    except Exception as e:
                        logger.debug(
                            "multi_universe_scanner_ticker_failed",
                            extra={"ticker": ticker, "error": str(e)},
                        )
                        return ticker, None, False

            results = await asyncio.gather(
                *[fetch_and_score(t, semaphore, client) for t in slice_tickers]
            )

            for ticker, signal, budget_deferred in results:
                if budget_deferred:
                    continue
                if signal is None:
                    runtime_state.scanner_state.tickers_failed_last_cycle.append(ticker)
                    fail_now_ms = int(time.time() * 1000)
                    runtime_state.scanner_state.cooldown_until_ms[ticker] = (
                        fail_now_ms + COOLDOWN_MS
                    )
                else:
                    all_signals.append(signal)

                    if runtime_config.ENABLE_PAPER_TRADING:
                        await _process_paper_intents(signal, client)

            runtime_state.scanner_state.markets_scanned_last_cycle = len(slice_tickers)

            all_signals.sort(
                key=lambda s: (
                    -max(
                        s.net_edge_yes if s.net_edge_yes is not None else float("-inf"),
                        s.net_edge_no if s.net_edge_no is not None else float("-inf"),
                    )
                )
            )
            top_signals = all_signals[:20]

            snapshot_ts_ms = int(time.time() * 1000)
            runtime_state.top_signals_snapshot = top_signals_snapshot_model(
                timestamp_ms=snapshot_ts_ms,
                signals=[signal_response_model(**signal_to_response(s)) for s in top_signals],
                market_count=len(market_tickers),
            )

            runtime_state.scanner_state.last_scan_ms = snapshot_ts_ms
            runtime_state.scanner_state.last_error = None

            cycle_end_ms = int(time.time() * 1000)
            cycle_duration_ms = cycle_end_ms - cycle_start_ms
            runtime_state.scanner_state.last_cycle_duration_ms = cycle_duration_ms

            logger.info(
                "multi_universe_scanner_cycle_complete",
                extra={
                    "total_market_count": len(market_tickers),
                    "enabled_universes": runtime_state.scanner_state.enabled_universes,
                    "discovered_universe_counts": runtime_state.scanner_state.discovered_universe_counts,
                    "slice_start": start_idx,
                    "slice_size": len(slice_tickers),
                    "signal_count": len(all_signals),
                    "top_count": len(top_signals),
                    "failed_count": len(runtime_state.scanner_state.tickers_failed_last_cycle),
                    "skipped_cooldown_count": len(
                        runtime_state.scanner_state.tickers_skipped_cooldown_last_cycle
                    ),
                    "duration_ms": cycle_duration_ms,
                },
            )

        except asyncio.CancelledError:
            logger.info("scanner_cancelled")
            break

        except Exception as e:
            logger.exception(
                "multi_universe_scanner_loop_error",
                extra={"error": str(e)},
            )
            runtime_state.scanner_state.last_error = str(e)

        cycle_end_ms = int(time.time() * 1000)
        cycle_duration_ms = cycle_end_ms - cycle_start_ms
        runtime_state.scanner_state.last_cycle_duration_ms = cycle_duration_ms
        sleep_ms = max(0, (TARGET_CYCLE_SECONDS * 1000) - cycle_duration_ms)
        await asyncio.sleep(sleep_ms / 1000.0)
