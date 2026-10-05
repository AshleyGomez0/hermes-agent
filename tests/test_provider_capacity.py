"""Offline unit tests: no model, gateway, credential or production board calls."""
import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor


def test_restart_persistence_and_provider_isolation(tmp_path):
    assert importlib.util.find_spec("hermes_cli.provider_capacity") is not None
    from hermes_cli.provider_capacity import CapacityBreaker
    path = tmp_path / "capacity.sqlite"
    first = CapacityBreaker(path, enabled=True)
    assert first.acquire("owner", "A", now=100).allowed
    first.rate_limited("owner", "A", now=100, board="attribution-only")
    restarted = CapacityBreaker(path, enabled=True)
    assert not restarted.acquire("owner", "A", now=101).allowed
    assert restarted.acquire("owner", "B", now=101).allowed


def test_exactly_one_probe_across_parallel_pollers(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    path = tmp_path / "capacity.sqlite"
    CapacityBreaker(path, enabled=True).rate_limited("shared-root-account", "A", now=100)
    def contender(_):
        return CapacityBreaker(path, enabled=True).acquire("shared-root-account", "A", now=401)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(contender, range(32)))
    assert sum(result.allowed for result in results) == 1
    probe = next(result for result in results if result.allowed)
    assert probe.state == "HALF_OPEN" and probe.probe_token


def test_probe_success_and_stale_result_safety(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    b = CapacityBreaker(tmp_path / "capacity.sqlite", enabled=True)
    b.rate_limited("owner", "A", now=100)
    old = b.acquire("owner", "A", now=401)
    assert old.allowed
    newer = b.acquire("owner", "A", now=462)
    assert newer.allowed and newer.probe_token != old.probe_token
    assert not b.success("owner", "A", old.probe_token, now=463)
    b.rate_limited("owner", "A", now=464)
    assert not b.success("owner", "A", newer.probe_token, now=465)
    assert not b.acquire("owner", "A", now=465).allowed
    probe = b.acquire("owner", "A", now=1065)
    assert b.success("owner", "A", probe.probe_token, now=1066)
    assert b.acquire("owner", "A", now=1067).state == "CLOSED"


def test_known_reset_max_and_unknown_finite_backoff(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    b = CapacityBreaker(tmp_path / "capacity.sqlite", enabled=True)
    b.rate_limited("owner", "A", now=100, reset_at=5000)
    assert b.acquire("owner", "A", now=400).eligible_at == 5000
    b.rate_limited("owner", "A", now=101, reset_at=900)
    assert b.acquire("owner", "A", now=901).eligible_at == 5000
    b.rate_limited("owner", "B", now=100)
    assert b.acquire("owner", "B", now=400).allowed


def test_disabled_never_reads_or_writes_state(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    path = tmp_path / "missing-directory" / "capacity.sqlite"
    b = CapacityBreaker(path, enabled=False)
    assert b.acquire("owner", "A", now=100).allowed
    assert not b.rate_limited("owner", "A", now=100)
    assert not b.success("owner", "A", "stale", now=100)
    assert not path.exists()


def test_malformed_state_and_reset_fail_closed(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    path = tmp_path / "capacity.sqlite"
    b = CapacityBreaker(path, enabled=True)
    b.rate_limited("owner", "A", now=100)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE capacity SET state='BROKEN'")
    assert not b.acquire("owner", "A", now=99999).allowed
    assert not b.rate_limited("owner", "A", now=100, reset_at=float("nan"))
    assert not b.acquire("", "A", now=99999).allowed


def test_malformed_config_fail_closed(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    b = CapacityBreaker(tmp_path / "capacity.sqlite", enabled="yes")
    assert not b.acquire("owner", "A", now=100).allowed


def test_success_expired_lease_cannot_close(tmp_path):
    from hermes_cli.provider_capacity import CapacityBreaker
    b = CapacityBreaker(tmp_path / "capacity.sqlite", enabled=True)
    b.rate_limited("owner", "A", now=100)
    probe = b.acquire("owner", "A", now=400)
    assert not b.success("owner", "A", probe.probe_token, now=460)
