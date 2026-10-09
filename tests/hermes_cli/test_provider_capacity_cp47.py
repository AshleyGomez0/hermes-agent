"""CP47 capacity canaries: explicit scope, shared provider quota, bounded half-open probes."""

from __future__ import annotations

import math

from hermes_cli.provider_capacity import CapacityBreaker


def test_disabled_breaker_preserves_legacy_route_without_io(tmp_path):
    ledger = tmp_path / "capacity.sqlite"
    breaker = CapacityBreaker(ledger, enabled=False)
    permit = breaker.acquire("ashley", "minimax", now=1000)
    assert permit.allowed and permit.state == "DISABLED"
    assert not breaker.rate_limited("ashley", "minimax", now=1000)
    assert not ledger.exists()


def test_429_opens_shared_owner_provider_quota_and_fences_single_probe(tmp_path):
    breaker = CapacityBreaker(tmp_path / "capacity.sqlite", enabled=True)
    assert breaker.acquire("ashley", "minimax", now=1000).allowed
    assert breaker.rate_limited("ashley", "minimax", now=1000, board="suini")
    # Separate board does not get another provider admission under shared quota.
    denied = breaker.acquire("ashley", "minimax", now=1299)
    assert not denied.allowed and denied.state == "OPEN"
    granted = breaker.acquire("ashley", "minimax", now=1300)
    assert granted.allowed and granted.state == "HALF_OPEN"
    assert granted.probe_token
    assert not breaker.acquire("ashley", "minimax", now=1300).allowed
    assert breaker.bind_probe(
        "ashley", "minimax", granted.probe_token,
        board="david-os", run_id="315", now=1301,
    )
    assert not breaker.acquire(
        "ashley", "minimax", now=1361, probe_alive=lambda board, run: True
    ).allowed
    # Expired lease is not sufficient without evidence that probe finished.
    assert not breaker.acquire("ashley", "minimax", now=1361).allowed
    successor = breaker.acquire(
        "ashley", "minimax", now=1361, probe_alive=lambda board, run: False,
    )
    assert successor.allowed and successor.probe_token != granted.probe_token
    assert not breaker.success("ashley", "minimax", granted.probe_token, now=1362)
    assert breaker.bind_probe(
        "ashley", "minimax", successor.probe_token,
        board="suini", run_id="316", now=1362,
    )
    assert breaker.success("ashley", "minimax", successor.probe_token, now=1363)
    assert breaker.acquire("ashley", "minimax", now=1363).state == "CLOSED"


def test_rate_limited_reset_and_malformed_inputs_fail_closed(tmp_path):
    breaker = CapacityBreaker(tmp_path / "capacity.sqlite", enabled=True)
    assert not breaker.acquire("", "minimax", now=1000).allowed
    assert not breaker.acquire("ashley", "minimax", now=math.inf).allowed
    assert not breaker.acquire("ashley", "minimax", now=10 ** 500).allowed
    assert not breaker.rate_limited(
        "ashley", "minimax", now=1000, reset_at=math.nan,
    )
    assert not breaker.rate_limited(
        "ashley", "minimax", now=1000, reset_at=10 ** 500,
    )
    assert breaker.rate_limited(
        "ashley", "minimax", now=1000, board="suini", reset_at=2500,
    )
    assert not breaker.acquire("ashley", "minimax", now=2499).allowed
    assert breaker.acquire("ashley", "minimax", now=2500).allowed


def test_unavailable_ledger_denies_admission(tmp_path):
    invalid = CapacityBreaker(tmp_path / "missing" / "capacity.sqlite", enabled=True)
    permit = invalid.acquire("ashley", "minimax", now=1000)
    assert not permit.allowed and permit.state == "INVALID"
