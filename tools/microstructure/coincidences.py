"""One-to-one exchange-millisecond coincidences in valid overlapping feed segments."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Bucket:
    connection: int
    epoch: int
    snapshot_record: int
    ticker: str
    ts_ms: int
    side: str
    price_cents: Decimal


@dataclass
class Candidates:
    decreases: list[tuple[Decimal, dict]] = field(default_factory=list)
    prints: list[tuple[Decimal, str, dict]] = field(default_factory=list)


class Coincidences:
    def __init__(self) -> None:
        self.groups: dict[Bucket, Candidates] = defaultdict(Candidates)
        self.conflicting_connections: set[int] = set()
        self.conflicting_ids: set[str] = set()
        self.uncertain_prints: set[tuple[int, str, int]] = set()
        self.untimed: set[tuple[int, int, str]] = set()

    def report(self) -> tuple[list[dict], dict]:
        matches = []
        unresolved: Counter[str] = Counter()
        for key, group in self.groups.items():
            if not group.prints or not group.decreases:
                continue
            if (
                key.connection in self.conflicting_connections
                or any(tid in self.conflicting_ids for _, tid, _ in group.prints)
                or (key.connection, key.ticker, key.ts_ms) in self.uncertain_prints
                or (key.connection, key.epoch, key.ticker) in self.untimed
            ):
                unresolved["conflicting_or_incomplete_evidence"] += 1
                continue
            if len(group.prints) != 1 or len(group.decreases) != 1:
                unresolved["multiple_prints_or_decreases"] += 1
                continue
            qty, trade_id, trade = group.prints[0]
            removed, delta = group.decreases[0]
            if qty != removed:
                unresolved["unequal_quantities"] += 1
                continue
            matches.append(
                {
                    "trade_id": trade_id,
                    "market_ticker": key.ticker,
                    "resting_side": key.side,
                    "price_cents": format(key.price_cents, "f"),
                    "quantity_fp": format(qty, "f"),
                    "ts_ms": key.ts_ms,
                    "connection": key.connection,
                    "segment": key.epoch,
                    "snapshot_record": key.snapshot_record,
                    "trade": trade,
                    "decrease": delta,
                }
            )
        return sorted(matches, key=lambda m: m["trade"]["record"]), dict(unresolved)
