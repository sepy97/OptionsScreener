"""The per-user cache: the partition, the clock, and the bounds."""

from __future__ import annotations

from wheel_screener.api.usercache import PerUserCache


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cache(ttl=30.0, **kw) -> tuple[PerUserCache, _Clock]:
    clock = _Clock()
    return PerUserCache(ttl, clock=clock, **kw), clock


# --- the partition --------------------------------------------------------------------------

def test_one_users_value_is_unreachable_from_another() -> None:
    cache, _ = _cache()
    cache.put("alice", "balances", [1, 2, 3])
    assert cache.get("alice", "balances") == [1, 2, 3]
    assert cache.get("bob", "balances") is None  # the same key, a different person


def test_the_same_contract_held_by_two_people_is_two_entries() -> None:
    """The swap cache's key really was identical for two people short the same put in the same
    size, so this is the collision the partition breaks."""
    cache, _ = _cache()
    key = ("MRVL", 1.0, 210.0, "2026-10-30")
    cache.put("alice", key, "swap")
    cache.put("bob", key, "keep")
    assert cache.get("alice", key) == "swap"
    assert cache.get("bob", key) == "keep"


def test_an_unidentified_caller_neither_reads_nor_writes() -> None:
    """No session means no partition. Silently sharing one would be the bug this prevents."""
    cache, _ = _cache()
    cache.put(None, "balances", ["leaked"])
    assert cache.get(None, "balances") is None
    assert len(cache) == 0


# --- the clock ------------------------------------------------------------------------------

def test_a_value_expires_after_the_ttl() -> None:
    cache, clock = _cache(ttl=30.0)
    cache.put("alice", "balances", "fresh")
    clock.advance(29.9)
    assert cache.get("alice", "balances") == "fresh"
    clock.advance(0.2)
    assert cache.get("alice", "balances") is None


def test_a_falsy_value_is_still_a_hit() -> None:
    """An account list can legitimately be empty, and re-reading it every request would spend
    upstream calls to learn the same nothing."""
    cache, _ = _cache()
    cache.put("alice", "balances", [])
    assert cache.get("alice", "balances") == []


# --- clearing -------------------------------------------------------------------------------

def test_clearing_one_user_leaves_the_others_alone() -> None:
    cache, _ = _cache()
    cache.put("alice", "balances", "a")
    cache.put("bob", "balances", "b")
    cache.clear("alice")
    assert cache.get("alice", "balances") is None
    assert cache.get("bob", "balances") == "b"


# --- the bounds -----------------------------------------------------------------------------

def test_a_users_entries_are_capped_oldest_first() -> None:
    """The verdict cache grew for the life of the process and was emptied only by Refresh."""
    cache, _ = _cache(max_entries_per_user=3)
    for i in range(5):
        cache.put("alice", i, i)
    assert cache.get("alice", 0) is None and cache.get("alice", 1) is None
    assert [cache.get("alice", i) for i in (2, 3, 4)] == [2, 3, 4]


def test_the_number_of_users_is_capped_least_recently_used_first() -> None:
    cache, _ = _cache(max_users=2)
    cache.put("alice", "k", "a")
    cache.put("bob", "k", "b")
    cache.get("alice", "k")  # alice is now the more recently used
    cache.put("carol", "k", "c")
    assert cache.get("bob", "k") is None  # bob was the quietest, so bob went
    assert cache.get("alice", "k") == "a" and cache.get("carol", "k") == "c"
    assert len(cache) == 2


def test_reading_an_entry_keeps_it_from_being_evicted() -> None:
    cache, _ = _cache(max_entries_per_user=2)
    cache.put("alice", "keep", 1)
    cache.put("alice", "drop", 2)
    cache.get("alice", "keep")  # touched, so "drop" is now the oldest
    cache.put("alice", "new", 3)
    assert cache.get("alice", "drop") is None
    assert cache.get("alice", "keep") == 1 and cache.get("alice", "new") == 3
