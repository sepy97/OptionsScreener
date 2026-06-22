from __future__ import annotations

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
