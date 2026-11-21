"""Offline B2 research on immutable B1 snapshots."""

from enum import StrEnum


class Mode(StrEnum):
    B2 = "b2"
    B1_QUEUE = "b1-queue"
    CANONICAL = "canonical"


ENGINE_VERSION = "b2-receipt-replay-v1"
MATCH_MS = 25
