"""Optional, independently pinned public REST receipts on the capture's local clock."""

import hashlib
import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from operator import attrgetter
from pathlib import Path

from kalshi_edge_engine.engine.fees import FeeProfile, series_fee_profile


@dataclass(frozen=True)
class Receipt:
    index: int
    mono_ns: int
    wall_ns: int
    payload: dict


@dataclass
class FeeEvidence:
    sha256: str
    receipts: dict[tuple[str, str], list[Receipt]]

    @classmethod
    def read(cls, path: Path, expected_sha256: str) -> "FeeEvidence":
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise ValueError("Fee receipt bundle differs from trusted SHA-256")
        bundle = json.loads(raw, parse_float=Decimal)
        if bundle["format"] != "b3b2-local-get-receipts-v1":
            raise ValueError("Unsupported fee receipt format")
        receipts: dict[tuple[str, str], list[Receipt]] = defaultdict(list)
        for i, row in enumerate(bundle["receipts"]):
            journal_hash, endpoint = row["journal_sha256"], row["path"]
            mono, wall = row["receipt_mono_ns"], row["receipt_wall_ns"]
            if (
                type(mono) is not int
                or mono < 0
                or type(wall) is not int
                or wall <= 0
                or not isinstance(journal_hash, str)
                or len(journal_hash) != 64
                or not isinstance(endpoint, str)
                or not isinstance(row["response"], dict)
            ):
                raise ValueError("Malformed fee receipt")
            receipts[journal_hash, endpoint].append(Receipt(i, mono, wall, row["response"]))
        for group in receipts.values():
            group.sort(key=attrgetter("mono_ns"))
            if len({r.mono_ns for r in group}) != len(group):
                raise ValueError("Indistinguishable fee receipt times for one endpoint")
        return cls(digest, dict(receipts))

    def resolve(
        self, journal_hash: str, market: str, event: str, at: int
    ) -> tuple[FeeProfile, list[int]] | None:
        def recent(path: str) -> Receipt | None:
            group = self.receipts.get((journal_hash, path), [])
            i = bisect_right(group, at, key=attrgetter("mono_ns"))
            if i and at - group[i - 1].mono_ns < 60_000_000_000:
                return group[i - 1]
            return None

        m, e = recent("/markets/" + market), recent("/events/" + event)
        if m is None or e is None:
            return None
        market_data, event_data = m.payload.get("market"), e.payload.get("event")
        if (
            not isinstance(market_data, dict)
            or not isinstance(event_data, dict)
            or market_data.get("ticker") != market
            or market_data.get("event_ticker") != event
            or market_data.get("fee_waiver_expiration_time")
            or event_data.get("event_ticker") != event
        ):
            return None
        series = event_data.get("series_ticker")
        s = recent("/series/" + series) if isinstance(series, str) and series else None
        series_data = s.payload.get("series") if s else None
        if s is None or not isinstance(series_data, dict) or series_data.get("ticker") != series:
            return None
        profile = series_fee_profile(
            series_data, event_data, observed_ts_ms=min(r.wall_ns for r in (m, e, s)) // 1_000_000
        )
        return (profile, [r.index for r in (m, e, s)]) if profile else None
