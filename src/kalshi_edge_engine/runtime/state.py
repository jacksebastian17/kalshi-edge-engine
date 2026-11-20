"""Mutable runtime state for scanner and paper trading."""

import asyncio
from typing import Any

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.orderbook import LiquidationQuote
from kalshi_edge_engine.runtime.market_fees import MarketFees


class _ScannerState:
    """Mutable state for the background NBA scanner."""

    def __init__(self) -> None:
        self.enabled = False
        self.last_scan_ms: int | None = None
        self.markets_discovered = 0
        self.markets_scanned_last_cycle = 0
        self.last_cycle_duration_ms: int | None = None
        self.tickers_failed_last_cycle: list[str] = []
        self.tickers_skipped_cooldown_last_cycle: list[str] = []
        self.last_error: str | None = None
        self.rotating_index = 0
        self.market_tickers: list[str] = []
        self.ticker_universe_map: dict[str, str] = {}
        self.enabled_universes: list[str] = []
        self.discovered_universe_counts: dict[str, int] = {}
        self.last_universe_refresh_ms = 0
        self.cooldown_until_ms: dict[str, int] = {}

    def reset(self) -> None:
        """Reset scanner state."""
        self.last_scan_ms = None
        self.markets_discovered = 0
        self.markets_scanned_last_cycle = 0
        self.last_cycle_duration_ms = None
        self.tickers_failed_last_cycle = []
        self.tickers_skipped_cooldown_last_cycle = []
        self.last_error = None
        self.rotating_index = 0
        self.market_tickers = []
        self.ticker_universe_map = {}
        self.enabled_universes = []
        self.discovered_universe_counts = {}
        self.last_universe_refresh_ms = 0
        self.cooldown_until_ms = {}


top_signals_snapshot: Any | None = None
scanner_state = _ScannerState()
paper_store: PaperStore | None = None
paper_enabled: bool = False
paper_simulation_tasks: set[asyncio.Task[Any]] = set()
last_pnl_snapshot_ts_ms = 0
paper_ticker_cooldown: dict[str, int] = {}
paper_intent_timestamps: list[int] = []
paper_daily_loss_breached: bool = False
paper_liquidation_marks: dict[tuple[str, str], LiquidationQuote] = {}
market_fees: MarketFees | None = None
