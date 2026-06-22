"""On-disk response cache shared across runs (and with the future API server).

Stores parsed JSON keyed by a request string, with a TTL checked against a stored
timestamp. Replaces hishel (whose 1.x API churned); we control our own endpoints, so a
simple TTL'd JSON cache is sufficient, dependency-free, and fully testable.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path


class DiskCache:
    def __init__(
        self, base_dir: str, ttl_seconds: int, now: Callable[[], float] = time.time
    ) -> None:
        self._dir = Path(base_dir)
        self._ttl = ttl_seconds
        self._now = now

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def get(self, key: str) -> object | None:
        try:
            envelope = json.loads(self._path(key).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, dict) or "ts" not in envelope:
            return None
        if self._now() - envelope["ts"] > self._ttl:
            return None
        return envelope.get("data")

    def set(self, key: str, value: object) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(json.dumps({"ts": self._now(), "data": value}))
        except (OSError, TypeError):
            pass
