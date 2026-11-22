"""Request-scoped receipt references. A queued reference becomes durable only in the recorder."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any

EVIDENCE_VERSION = 2
MODEL = "canonical-queue-b3b1-v1"


@dataclass(frozen=True)
class Receipt:
    session_id: str
    sequence: int
    received_ns: int
    ticker: str
    kind: str
    source: str
    universe: str
    intent_id: str | None
    capture_state: str


_scope: ContextVar[list[Receipt] | None] = ContextVar("response_receipts", default=None)


@contextmanager
def receipt_scope() -> Iterator[list[Receipt]]:
    refs: list[Receipt] = []
    token = _scope.set(refs)
    try:
        yield refs
    finally:
        _scope.reset(token)


def publish(refs: Iterable[Receipt]) -> None:
    scope = _scope.get()
    if scope is not None:
        scope.extend(refs)


def references(refs: Iterable[Receipt]) -> list[dict[str, Any]]:
    return [asdict(ref) for ref in refs]
