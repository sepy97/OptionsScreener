from __future__ import annotations

import logging
from pathlib import Path

from wheel_screener.adapters.cache import DiskCache


def test_disk_cache_roundtrip(tmp_path: Path) -> None:
    cache = DiskCache(str(tmp_path), ttl_seconds=100, now=lambda: 1000.0)
    assert cache.get("missing") is None
    cache.set("k", {"a": 1, "b": [2, 3]})
    assert cache.get("k") == {"a": 1, "b": [2, 3]}


def test_disk_cache_expires(tmp_path: Path) -> None:
    clock = {"t": 1000.0}
    cache = DiskCache(str(tmp_path), ttl_seconds=100, now=lambda: clock["t"])
    cache.set("k", {"a": 1})
    clock["t"] = 1099.0
    assert cache.get("k") == {"a": 1}  # within TTL
    clock["t"] = 1101.0
    assert cache.get("k") is None  # past TTL


def test_disk_cache_survives_new_instance(tmp_path: Path) -> None:
    DiskCache(str(tmp_path), ttl_seconds=100, now=lambda: 1000.0).set("k", {"a": 1})
    # a fresh instance (simulating a new run) reads the same directory
    assert DiskCache(str(tmp_path), ttl_seconds=100, now=lambda: 1000.0).get("k") == {"a": 1}


def test_disk_cache_missing_key_is_silent(tmp_path: Path, caplog) -> None:
    cache = DiskCache(str(tmp_path), ttl_seconds=100, now=lambda: 1000.0)
    with caplog.at_level(logging.WARNING, logger="wheel_screener.adapters.cache"):
        assert cache.get("nope") is None  # ordinary miss
    assert caplog.records == []  # a miss must NOT warn


def test_disk_cache_write_failure_warns_once(tmp_path: Path, caplog) -> None:
    # point the cache at a path that can't be a directory (a file exists there)
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir")
    cache = DiskCache(str(blocker / "sub"), ttl_seconds=100, now=lambda: 1000.0)
    with caplog.at_level(logging.WARNING, logger="wheel_screener.adapters.cache"):
        cache.set("k", {"a": 1})  # must not raise
        cache.set("k2", {"b": 2})  # second failure must not re-warn
    assert sum("cache write failed" in r.message for r in caplog.records) == 1
    assert cache.get("k") is None  # still degrades gracefully to a miss


def test_disk_cache_corrupt_entry_warns(tmp_path: Path, caplog) -> None:
    cache = DiskCache(str(tmp_path), ttl_seconds=100, now=lambda: 1000.0)
    cache.set("k", {"a": 1})
    cache._path("k").write_text("{ not json")  # corrupt the stored entry
    with caplog.at_level(logging.WARNING, logger="wheel_screener.adapters.cache"):
        assert cache.get("k") is None
    assert any("corrupt cache entry" in r.message for r in caplog.records)
