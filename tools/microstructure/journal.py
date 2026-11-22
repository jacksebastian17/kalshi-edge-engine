"""Write-once pilot runs, independently verifiable file seals and offline inspection."""

import base64
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .feed import Feed

ROOT = Path.home() / "kalshi-microstructure-pilots"
FORMAT = "b3b2-public-ws-1"
MANIFEST = "manifest.json"
LOG = "messages.jsonl"
SEAL = "seal.json"
HEALTH = "health.jsonl"
STOP = "stop.request"
BACKUP_HASHES = "backup-hashes.json"


class RecorderError(RuntimeError):
    """Durable recording failed. The run must remain unsealed."""


def run_path(name: str) -> Path:
    if not re.fullmatch(r"b3b2-[a-zA-Z0-9_-]{1,72}", name):
        raise ValueError("Use a fresh pilot name beginning with b3b2-")
    path = ROOT / name
    if path.resolve() != path or path.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Expected a dedicated unlinked directory outside the repository")
    return path


def encoded(data: Any) -> bytes:
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def exclusive(path: Path, data: Any) -> None:
    with path.open("xb") as f:
        f.write(encoded(data))
        f.flush()
        os.fsync(f.fileno())


def digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def manifest(path: Path) -> dict:
    value: dict = json.loads((path / MANIFEST).read_bytes())
    markets = value.get("markets", [])
    if (
        value.get("format") != FORMAT
        or not isinstance(markets, list)
        or not 2 <= len(set(markets)) == len(markets) <= 4
        or not all(isinstance(t, str) and t.startswith("KXNBAGAME-") for t in markets)
    ):
        raise ValueError("Invalid pilot manifest")
    return value


class Writer:
    def __init__(self, path: Path):
        if {p.name for p in path.iterdir()} != {MANIFEST}:
            raise ValueError("Capture requires a fresh prepared run")
        self.path = path
        self.manifest_hash = digest(path / MANIFEST)
        self.feed = Feed(manifest(path)["markets"])
        self.count = 0
        self.start = time.monotonic_ns()
        self.next_sync = self.start
        self.file = (path / LOG).open("xb")
        try:
            self.status_file = (path / HEALTH).open("xb")
        except BaseException:
            self.file.close()
            raise

    def record(self, kind: str, value: Any = None) -> bool:
        ns, mono = time.time_ns(), time.monotonic_ns()
        stored = base64.b64encode(value).decode("ascii") if kind == "raw" else value
        try:
            self.file.write(encoded([self.count + 1, ns, mono, kind, stored]))
        except OSError as exc:
            raise RecorderError("Unable to append received event") from exc
        self.count += 1
        return self.feed.event(kind, value, ns, mono)

    def flush(self, force: bool = False) -> None:
        now = time.monotonic_ns()
        if not force and now < self.next_sync:
            return
        try:
            self._checkpoint()
        except OSError as exc:
            raise RecorderError("Unable to checkpoint the capture") from exc
        self.next_sync = now + 1_000_000_000

    def _checkpoint(self) -> None:
        self.file.flush()
        os.fsync(self.file.fileno())
        free = shutil.disk_usage(self.path).free
        self.status_file.write(
            encoded(
                {
                    **self.feed.health(),
                    "durable_records": self.count,
                    "durable_bytes": self.file.tell(),
                    "disk_free_bytes": free,
                    "wall_ns": time.time_ns(),
                }
            )
        )
        self.status_file.flush()
        os.fsync(self.status_file.fileno())
        if free < 64 * 1024**2 or self.file.tell() >= 1024**3:
            raise RecorderError("pilot storage limit")

    def close(self, reason: str | None) -> None:
        try:
            if reason is not None:
                self.record("closed", reason)
            self.flush(force=True)
        finally:
            self.file.close()
            self.status_file.close()
        if reason is not None:
            exclusive(
                self.path / SEAL,
                {
                    "format": FORMAT,
                    "manifest_sha256": self.manifest_hash,
                    "messages_sha256": digest(self.path / LOG),
                    "records": self.count,
                    "reason": reason,
                },
            )


def inspect(path: Path, *, require_seal: bool = True) -> dict:
    if path.resolve() != path:
        raise ValueError("Linked archive path")
    for name in (MANIFEST, LOG, SEAL, HEALTH, BACKUP_HASHES):
        file = path / name
        if file.resolve() != file or (file.exists() and file.stat().st_nlink != 1):
            raise ValueError("Linked research file")
    meta = manifest(path)
    sealed = (path / SEAL).exists()
    if require_seal and not sealed:
        raise ValueError("Unsealed capture")
    seal = json.loads((path / SEAL).read_bytes()) if sealed else None
    if seal is not None and (
        seal["format"] != FORMAT or seal["manifest_sha256"] != digest(path / MANIFEST)
    ):
        raise ValueError("Invalid manifest seal")
    feed = Feed(meta["markets"])
    count = 0
    first = last = started = collection_end = None
    last_kind = last_value = None
    partial = False
    h = hashlib.sha256()
    with (path / LOG).open("rb") as f:
        for line in f:
            h.update(line)
            if not line.endswith(b"\n") and not sealed:
                partial = True
                break
            n, ns, mono, kind, val = json.loads(line)
            if type(ns) is not int or type(mono) is not int:
                raise ValueError("Invalid journal receipt timestamp")
            if (
                type(n) is not int
                or n != count + 1
                or (last is not None and mono < last)
                or last_kind == "closed"
            ):
                raise ValueError("Invalid journal order")
            count = n
            if kind == "started" and n == 1:
                started = mono
            if kind == "connected" and first is None:
                first = mono
            if kind == "collection_ended":
                collection_end = mono
            last, last_kind, last_value = mono, kind, val
            feed.event(
                kind, base64.b64decode(val, validate=True) if kind == "raw" else val, ns, mono
            )
    if seal is not None and (
        seal["records"] != count
        or seal["messages_sha256"] != h.hexdigest()
        or last_kind != "closed"
        or seal["reason"] != last_value
    ):
        raise ValueError("Journal seal mismatch")
    end = collection_end if collection_end is not None else last
    elapsed = (end - first) / 1e9 if first is not None and end is not None else 0.0
    if path.name == "backup" or (path / BACKUP_HASHES).exists():
        expected = json.loads((path / BACKUP_HASHES).read_bytes())
        if expected != {n: digest(path / n) for n in (MANIFEST, LOG, SEAL, HEALTH)}:
            raise ValueError("Backup hash mismatch")
    checkpoint = latest_health(path)
    if sealed and (
        not checkpoint
        or checkpoint["durable_records"] != count
        or checkpoint["durable_bytes"] != (path / LOG).stat().st_size
    ):
        raise ValueError("Final recorder checkpoint differs from seal")
    session_seconds = (end - started) / 1e9 if end is not None and started is not None else elapsed
    return {
        **feed.report(sealed, session_seconds),
        "connected_window_seconds": elapsed,
        "session": meta["session"],
        "capture_build_provenance": {
            "status": "recorded" if meta.get("build") else "missing_capture_build_fingerprint",
            "manifest_build": meta.get("build"),
        },
        "durable_records": count,
        "partial_final_record": partial,
        "seal": seal,
        "journal_sha256": h.hexdigest(),
        "last_recorder_checkpoint": checkpoint,
    }


def backup(path: Path) -> Path:
    source = inspect(path)
    target = path / "backup"
    if target.exists():
        if inspect(target) != source:
            raise ValueError("Existing backup differs")
        return target
    tmp = path / ("partial-" + uuid.uuid4().hex)
    tmp.mkdir()
    names = (MANIFEST, LOG, SEAL, HEALTH)
    for name in names:
        with (path / name).open("rb") as src, (tmp / name).open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    exclusive(tmp / BACKUP_HASHES, {n: digest(path / n) for n in names})
    if inspect(tmp) != source:
        raise ValueError("Backup verification failed")
    tmp.rename(target)
    return target


def latest_health(path: Path) -> dict | None:
    latest = None
    if (path / HEALTH).exists():
        with (path / HEALTH).open("rb") as f:
            for line in f:
                if line.endswith(b"\n"):
                    latest = json.loads(line)
    return latest


def health(path: Path) -> dict:
    manifest(path)
    if (path / "recorder-failure.json").is_file():
        return {"state": "recorder_failed", "last_durable": latest_health(path)}
    if (path / SEAL).exists():
        return {
            "state": "sealed",
            "seal": json.loads((path / SEAL).read_bytes()),
            "last_durable": latest_health(path),
        }
    if not (path / LOG).exists():
        return {"state": "prepared"}
    latest = latest_health(path)
    stale = not latest or time.time_ns() - latest["wall_ns"] > 10_000_000_000
    return {"state": "stale_or_interrupted" if stale else "recording", "last_durable": latest}
