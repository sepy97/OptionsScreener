from __future__ import annotations

from wheel_screener.api.ratelimit import SlidingWindowLimiter, client_ip, is_expensive


def test_sliding_window_allows_then_blocks_then_recovers() -> None:
    lim = SlidingWindowLimiter(per_window=3, window_seconds=60)
    assert [lim.allow("a", 100.0) for _ in range(3)] == [True, True, True]  # budget of 3
    assert lim.allow("a", 100.5) is False  # 4th within the window is blocked
    assert lim.allow("b", 100.5) is True  # a different IP has its own budget
    assert lim.allow("a", 161.0) is True  # >60s later the window has slid; allowed again


def test_sweep_bounds_memory_under_one_off_ips() -> None:
    lim = SlidingWindowLimiter(per_window=1, window_seconds=60, max_keys=5)
    for i in range(20):  # 20 distinct one-off IPs at t=0
        lim.allow(f"ip{i}", 0.0)
    # a later call past the window triggers the sweep of fully-expired keys
    lim.allow("fresh", 100.0)
    assert len(lim._hits) <= 6  # swept down to ~the fresh key, not 20+


def test_is_expensive_only_matches_start_endpoints() -> None:
    assert is_expensive("POST", "/screen") and is_expensive("POST", "/search")
    assert is_expensive("POST", "/runs") and is_expensive("GET", "/search/export.csv")
    # cheap reads / control endpoints are NOT throttled
    assert not is_expensive("GET", "/runs/j/progress")  # the 2s poll
    assert not is_expensive("GET", "/health")
    assert not is_expensive("POST", "/runs/j/cancel")
    assert not is_expensive("GET", "/runs/j/export.csv")  # exports a STORED run (no fresh pull)


def test_client_ip_uses_last_hop_not_spoofable_first() -> None:
    # Caddy appends the true client last; a client-forged first hop must NOT be trusted
    assert client_ip("1.2.3.4, 10.0.0.1", "172.18.0.2") == "10.0.0.1"  # last hop = real client
    assert client_ip("evil-spoof, 8.8.8.8", "172.18.0.2") == "8.8.8.8"  # forged first ignored
    assert client_ip(None, "172.18.0.2") == "172.18.0.2"  # no proxy header -> direct peer
    assert client_ip("", "9.9.9.9") == "9.9.9.9"
