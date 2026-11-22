"""Clock evidence from journal receipts. All subtraction precedes unit conversion."""

import os
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from typing import Any


def windows_clock_status() -> dict:
    observation: dict = {"observed_wall_ns": time.time_ns()}
    if os.name != "nt":
        return {**observation, "status": "unavailable_on_platform"}
    try:
        result = subprocess.run(
            ["w32tm", "/query", "/status", "/verbose"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**observation, "status": "unavailable", "reason": type(exc).__name__}
    return {
        **observation,
        "status": "queried" if result.returncode == 0 else "unavailable",
        "returncode": result.returncode,
        "output": (result.stdout + result.stderr).strip(),
    }


def summary(samples_ns: list[int]) -> dict:
    values = sorted(samples_ns)
    return {
        "samples": len(values),
        "negative_samples": sum(x < 0 for x in values),
        "min": values[0] / 1e6 if values else None,
        "median": statistics.median(values) / 1e6 if values else None,
        "p95": values[(95 * len(values) - 1) // 100] / 1e6 if values else None,
        "max": values[-1] / 1e6 if values else None,
    }


class Timing:
    def __init__(self) -> None:
        self.start: tuple[int, int] | None = None
        self.wall_minus_mono = 0
        self.low = self.high = 0
        self.samples: list[tuple[str, int | None, int]] = []
        self.issues: dict[str, Counter[str]] = defaultdict(Counter)
        self.diagnostics: list[dict] = []

    def receipt(self, wall: int, mono: int | None) -> None:
        if mono is None:
            return
        if self.start is None:
            self.start = (wall, mono)
        self.wall_minus_mono = (wall - self.start[0]) - (mono - self.start[1])
        self.low = min(self.low, self.wall_minus_mono)
        self.high = max(self.high, self.wall_minus_mono)

    def observe(self, kind: str, packet: dict, wall: int, mono: int | None) -> None:
        sent = packet.get("sending_ts_ms")
        if type(sent) is not int or not 0 <= sent <= 2**63 - 1:
            self.issues[kind]["missing" if "sending_ts_ms" not in packet else "malformed"] += 1
            return
        elapsed = mono - self.start[1] if mono is not None and self.start else None
        self.samples.append((kind, elapsed, wall - sent * 1_000_000))

    def report(self) -> dict[str, Any]:
        kinds = ("orderbook_snapshot", "orderbook_delta", "trade")
        by_kind = {k: summary([v for kind, _, v in self.samples if kind == k]) for k in kinds}
        bins: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for kind, elapsed, value in self.samples:
            if elapsed is not None:
                bins[elapsed // 120_000_000_000 * 120][kind].append(value)
        total = summary([v for _, _, v in self.samples])
        return {
            **total,
            "interpretation": "clock_alignment_required"
            if total["negative_samples"]
            else "uncalibrated_wall_clock_difference",
            "by_message_type": by_kind,
            "by_channel": {
                "orderbook_delta": summary([v for k, _, v in self.samples if k != "trade"]),
                "trade": by_kind["trade"],
            },
            "two_minute_windows": [
                {"from_elapsed_seconds": window, **{k: summary(group[k]) for k in kinds}}
                for window, group in sorted(bins.items())
            ],
            "sending_timestamp_issues": {k: dict(c) for k, c in self.issues.items()},
            "wall_elapsed_minus_monotonic_elapsed_ms": {
                "min": self.low / 1e6,
                "end": self.wall_minus_mono / 1e6,
                "max": self.high / 1e6,
            }
            if self.start
            else None,
            "host_clock_observations": self.diagnostics,
        }
