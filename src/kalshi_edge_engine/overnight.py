"""Offline preparation and backup of an isolated B3A paper run."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from kalshi_edge_engine.db.market_recorder import DISK_RESERVE, MIB, MarketRecorder
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.runtime import config

EXPERIMENT = "canonical_fixed_point_b3a_v1"
BASE = "3ac1a78fdd531af6553f2be16f446c6a14c89e28"
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "experiment.json"


def requested() -> bool:
    return os.getenv("PAPER_EXPERIMENT_ID") == EXPERIMENT or os.getenv(
        "MARKET_RECORDER_APPEND_ONLY", "false"
    ).lower() not in {"false", "0", "no", ""}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def revision() -> dict[str, str]:
    if Path.cwd().resolve() != ROOT or Path(git("rev-parse", "--show-toplevel")).resolve() != ROOT:
        raise ValueError("Run from the repository containing the imported B3A source")
    if git("branch", "--show-current") != "main":
        raise ValueError("Overnight capture requires the B3A checkout on main")
    git("merge-base", "--is-ancestor", BASE, "HEAD")
    git(
        "diff",
        "--quiet",
        BASE,
        "--",
        "src/kalshi_edge_engine/engine",
        "src/kalshi_edge_engine/kalshi",
        "src/kalshi_edge_engine/runtime",
    )
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/kalshi_edge_engine").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return {"commit": git("rev-parse", "HEAD"), "source_sha256": digest.hexdigest()}


def paper_policy() -> dict[str, Any]:
    if not config.ENABLE_PAPER_TRADING or not config.get_enabled_universes():
        raise ValueError("Paper simulation and at least one configured universe are required")
    if os.getenv("ENABLE_LIVE_TRADING", "false").lower() not in {"false", "0", "no"}:
        raise ValueError("Live trading must be disabled")
    effective = {
        u: asdict(config.get_effective_paper_config(u)) for u in config.get_enabled_universes()
    }
    if (
        config.PAPER_ALLOW_PRICE_IMPROVES
        or config.PAPER_ALLOW_MOVED_THROUGH
        or any(p["allow_price_improves"] or p["allow_moved_through"] for p in effective.values())
    ):
        raise ValueError(
            "Disable price-improving and moved-through fills globally and in every enabled universe"
        )
    baseline = dotenv_values(ROOT / ".env")
    for key in set(os.environ) | baseline.keys():
        value = os.getenv(key)
        if key in {"PAPER_DB_PATH", "PAPER_EXPERIMENT_ID"} or key.endswith(
            ("_ALLOW_MOVED_THROUGH", "_ALLOW_PRICE_IMPROVES")
        ):
            continue
        if key.startswith(("PAPER_", "KALSHI_READ_", "ENABLE_UNIVERSE_")) and value != baseline.get(
            key
        ):
            raise ValueError(f"Unexpected policy override relative to repository .env: {key}")
    return {
        "universes": effective,
        "globals": {k: v for k, v in vars(config).items() if k.startswith("PAPER_")},
        "read_budget": {
            k: os.getenv(k)
            for k in (
                "KALSHI_READ_TIER",
                "KALSHI_READ_TOKENS_PER_SECOND",
                "KALSHI_READ_BUCKET_CAPACITY",
                "KALSHI_READ_QUEUE_TIMEOUT_SECONDS",
                "KALSHI_READ_ENDPOINT_COSTS_JSON",
            )
        },
        "exchange_client": "GET only",
        "entry_execution": "paper simulator",
    }


@dataclass(frozen=True)
class Run:
    ledger: Path
    recorder: Path

    @classmethod
    def configured(cls) -> "Run":
        if os.getenv("PAPER_EXPERIMENT_ID") != EXPERIMENT:
            raise ValueError(f"Expected dedicated paper experiment {EXPERIMENT}")
        paths = []
        for env, name in (
            ("PAPER_DB_PATH", "paper_trading.db"),
            ("MARKET_RECORDER_DB", "market_data_recorder.db"),
        ):
            raw = Path(os.environ.get(env, ""))
            path = raw.resolve()
            if (
                not raw.is_absolute()
                or raw != path
                or path.name != name
                or path.is_relative_to(ROOT)
            ):
                raise ValueError(
                    f"{env} requires an isolated absolute path outside the repository ending in {name}"
                )
            paths.append(path)
        run = cls(*paths)
        if run.ledger.parent != run.recorder.parent or not run.directory.is_dir():
            raise ValueError("Both databases require the same dedicated, existing run directory")
        for file in run.directory.iterdir():
            if (
                file.is_symlink()
                or file.resolve() != file
                or (file.is_file() and file.stat().st_nlink != 1)
            ):
                raise ValueError(f"Linked files are unsafe in the run directory: {file.name}")
        if not (run.directory / MANIFEST).exists() and any(
            p.name != run.ledger.name + ".paper-worker.lock" for p in run.directory.iterdir()
        ):
            raise ValueError("New experiment requires an empty directory; existing files refused")
        return run

    @property
    def directory(self) -> Path:
        return self.ledger.parent

    def metadata(self, recorder: MarketRecorder | None) -> dict[str, Any]:
        if recorder is None or not recorder.append_only or Path(recorder.path) != self.recorder:
            raise ValueError("An enabled append-only recorder is required")
        if recorder.max_bytes < 4096 * MIB:
            raise ValueError("Overnight recorder requires at least 4096 MiB")
        return {
            **revision(),
            "experiment_id": EXPERIMENT,
            "paper_db": str(self.ledger),
            "recorder_db": str(self.recorder),
            "append_only": True,
            "max_bytes": recorder.max_bytes,
            "disk_reserve_bytes": DISK_RESERVE,
            "policy": paper_policy(),
        }

    def verify(self, metadata: dict[str, Any]) -> None:
        if self.saved() != json.loads(json.dumps(metadata)):
            raise ValueError("Experiment configuration or code differs from saved preflight")
        if not self.ledger.is_file() or not self.recorder.is_file():
            raise ValueError("Prepared run is missing a database")
        with closing(readonly(self.ledger)) as db:
            if db.execute("SELECT id FROM paper_experiments").fetchall() != [(EXPERIMENT,)]:
                raise ValueError("Ledger experiment identity mismatch")

    def saved(self) -> dict[str, Any]:
        path = self.directory / MANIFEST
        if not path.is_file():
            raise ValueError("Run preflight first to prepare a fresh experiment directory")
        return dict(json.loads(path.read_text(encoding="utf-8")))


def readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


async def preflight() -> dict[str, Any]:
    run = Run.configured()
    recorder = MarketRecorder.from_env(lambda _: "UNKNOWN")
    metadata = run.metadata(recorder)
    assert recorder is not None
    async with KalshiClient():
        pass  # Loads the configured signing key and read budget without an exchange call.
    with PaperStore.claim_database(str(run.ledger)):
        manifest = run.directory / MANIFEST
        if manifest.exists():
            run.verify(metadata)
        elif any(p.name != run.ledger.name + ".paper-worker.lock" for p in run.directory.iterdir()):
            raise ValueError("New experiment requires an empty directory; existing files refused")
        task = asyncio.create_task(recorder.run())
        try:
            await recorder.initialized.wait()
            recorder.require_healthy()
            store = PaperStore(str(run.ledger), experiment_id=EXPERIMENT)
            counts = {"intents": store.count_intents_total(), "fills": store.count_fills_total()}
            if not manifest.exists() and any(counts.values()):
                raise ValueError("New ledger contains existing activity")
        finally:
            recorder.stop.set()
            await task
        if recorder.failure:
            raise RuntimeError(recorder.failure)
        if not manifest.exists():
            with manifest.open("x", encoding="utf-8") as stream:
                json.dump(metadata, stream, indent=2, sort_keys=True)
        return {
            "preflight": "PASS",
            "experiment_id": EXPERIMENT,
            "ledger": str(run.ledger),
            "recorder": str(run.recorder),
            **counts,
            "retained": recorder.retained,
            "recorder_probe": "ready and drained",
            "disk_free_bytes": recorder.disk_free_bytes,
            "commit": metadata["commit"],
            "source_sha256": metadata["source_sha256"],
        }


def backup() -> Path:
    run = Run.configured()
    with PaperStore.claim_database(str(run.ledger)):
        saved = run.saved()
        if saved.get("paper_db") != str(run.ledger) or saved.get("recorder_db") != str(
            run.recorder
        ):
            raise ValueError("Backup paths differ from saved metadata")
        files = (run.ledger, run.recorder)
        required = sum(f.stat().st_size for f in run.directory.glob("*.db*"))
        if shutil.disk_usage(run.directory).free < required + DISK_RESERVE:
            raise OSError("Insufficient free space for offline backup")
        dest = run.directory / ("backup-" + uuid.uuid4().hex)
        partial = dest.with_suffix(".partial")
        partial.mkdir()
        for file in files:
            with (
                closing(readonly(file)) as src,
                closing(sqlite3.connect(partial / file.name)) as dst,
            ):
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError(f"Backup integrity check failed: {file.name}")
        (partial / MANIFEST).write_text(json.dumps(saved, indent=2), encoding="utf-8")
        with closing(readonly(partial / run.recorder.name)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute(
                """WITH ordered AS (
                       SELECT session_id,sequence,
                              row_number() OVER (PARTITION BY session_id ORDER BY id) AS expected
                       FROM market_events
                   ), receipts AS (
                       SELECT session_id, count(*) AS rows, sum(sequence != expected) AS mismatches
                       FROM ordered GROUP BY session_id
                   )
                   SELECT s.*, coalesce(r.rows, 0) AS receipt_rows,
                          coalesce(r.mismatches, 0) AS sequence_mismatches
                   FROM capture_sessions s LEFT JOIN receipts r ON s.id = r.session_id
                   ORDER BY s.rowid"""
            ):
                complete = (
                    row["state"] == "clean"
                    and row["sequence_mismatches"] == 0
                    and row["received"] == row["written"] == row["receipt_rows"]
                    and row["dropped"] == row["errors"] == 0
                )
                print(json.dumps({**dict(row), "complete_recorded_session": complete}))
        partial.rename(dest)
        return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "backup"])
    args = parser.parse_args()
    try:
        if args.action == "preflight":
            print(json.dumps(asyncio.run(preflight()), indent=2))
        else:
            print(backup())
    except Exception as exc:
        sys.exit(f"NO-GO: {exc}")


if __name__ == "__main__":
    main()
