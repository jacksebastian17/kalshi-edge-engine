"""Reviewed complete capture fingerprints. The approval registry is outside the hashed recorder."""

import hashlib
from pathlib import Path

from tools.microstructure import journal

LEGACY_BUILD = "90bbee4f819d93ee920b7b1c42015a102ec7c543e1d2fd9bdbfc1b9b3e3f877e"
DIVERSE_BUILD = "f5403fd6efa88aa2d1c1c596db7519945ceade9948eac4d9f81deed1dad9e32c"
APPROVED = {
    LEGACY_BUILD: {"capture_commit": "cfba2d7773250e0f2e4e3442b1abf9a944cad4b1"},
    DIVERSE_BUILD: {"capture_release": "b3b2-stage2b1-v1"},
}


def approved_capture(build: dict) -> dict[str, str]:
    fingerprint = hashlib.sha256(journal.encoded(build)).hexdigest()
    if fingerprint not in APPROVED:
        raise ValueError(f"Unapproved capture build: {fingerprint}")
    return {"capture_build_sha256": fingerprint, **APPROVED[fingerprint]}


def verify_replay_modules(build: dict) -> None:
    # Acquisition and CLI can evolve independently. All remaining modules are replay dependencies.
    acquisition = {"pilot.py", "__main__.py"}
    local = {
        p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for p in Path(journal.__file__).parent.glob("*.py")
        if p.name not in acquisition
    }
    if local != {k: v for k, v in build["module_sha256"].items() if k not in acquisition}:
        raise ValueError("Local canonical replay modules differ from the approved capture build")
