"""On-disk response cache shared across runs (and with the future API server).

Stores parsed JSON keyed by a request string, with a TTL checked against a stored
timestamp. Replaces hishel (whose 1.x API churned); we control our own endpoints, so a
simple TTL'd JSON cache is sufficient, dependency-free, and fully testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class DiskCache:
    def __init__(
        self, base_dir: str, ttl_seconds: int, now: Callable[[], float] = time.time
    ) -> None:
        self._dir = Path(base_dir)
        self._ttl = ttl_seconds
        self._now = now
        self._warned: set[str] = set()  # de-dupe warnings so a broken cache dir doesn't spam

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def _warn_once(self, dedupe_key: str, message: str) -> None:
        if dedupe_key not in self._warned:
            self._warned.add(dedupe_key)
            logger.warning(message)

    def get(self, key: str) -> object | None:
        path = self._path(key)
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return None  # ordinary cache miss — not an error
        except OSError as e:
            self._warn_once(f"read:{self._dir}", f"cache read failed in {self._dir}: {e}")
            return None
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as e:
            self._warn_once(f"corrupt:{path}", f"discarding corrupt cache entry {path}: {e}")
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
        except (OSError, TypeError) as e:
            # a non-writable dir / unserializable value disables caching — say so, once,
            # rather than silently re-paying the full (rate-limited) API cost every call
            self._warn_once(f"write:{self._dir}", f"cache write failed in {self._dir}; "
                            f"caching disabled for this path: {e}")
