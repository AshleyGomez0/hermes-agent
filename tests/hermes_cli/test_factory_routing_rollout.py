"""Guarded Factory policy enrollment on a shared, single-dispatcher board."""

import json
from pathlib import Path

import pytest

from hermes_cli import config as config_module
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {}})
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def configured_policy(board, tmp_path, monkeypatch):
    enrolled = kb.create_task(board, title="enrolled readonly CP canary", assignee="codex-worker")
    legacy = kb.create_task(board, title="existing ordinary project work", assignee="codex-worker")
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET provider_override=?, model_override=? WHERE id=?",
            ("openai-codex", "gpt-6.1-sol", enrolled),
        )
    db_path = next(row[2] for row in board.execute("PRAGMA database_list") if row[1] == "main")
    policy = {
        "schema_version": 1,
        "enabled": True,
        "owner_home": str((tmp_path / ".hermes").resolve()),
        "board_db": str(Path(db_path).resolve()),
        "routes": {
            "codex": {
                "profile": "codex-worker",
                "provider": "openai-codex",
                "model": "gpt-6.1-sol",
                "eligible_roles": ["read_only", "writer", "test_fix", "independent_reviewer"],
            },
            "minimax": {"preserved": True},
        },
        "require_independent_reviewer": True,
        "qwen": {"bounded_only": True, "tools": False},
        "deterministic": "scripts",
        "task_roles": {enrolled: {"role": "read_only"}},
        "rollout": {"mode": "task_allowlist", "task_ids": [enrolled]},
    }
    path = tmp_path / "factory-policy.json"
    monkeypatch.setattr(
        config_module, "load_config",
        lambda: {"kanban": {"factory_routing_policy_path": str(path)}},
    )

    def save():
        path.write_text(json.dumps(policy), encoding="utf-8")

    save()
    return enrolled, legacy, policy, save


def test_task_allowlist_applies_provider_guard_to_enrolled_only(board, tmp_path, monkeypatch):
    enrolled, legacy, _, _ = configured_policy(board, tmp_path, monkeypatch)
    context = {}
    assert dispatch._factory_policy_guard(board, enrolled, context) is None
    assert context["provider"] == "openai-codex"
    assert context["model"] == "gpt-6.1-sol"
    assert context["profile"] == "codex-worker"
    assert context["role"] == "read_only"
    assert context["board"] == str(Path(next(
        row[2] for row in board.execute("PRAGMA database_list") if row[1] == "main"
    )).resolve())
    legacy_context = {}
    assert dispatch._factory_policy_guard(board, legacy, legacy_context) is None
    assert legacy_context == {}


def test_missing_enrolled_contract_fails_closed_without_freezing_other_task(board, tmp_path, monkeypatch):
    enrolled, legacy, policy, save = configured_policy(board, tmp_path, monkeypatch)
    policy["task_roles"].clear()
    save()
    assert dispatch._factory_policy_guard(board, enrolled) == "factory_missing_role_adapter"
    assert dispatch._factory_policy_guard(board, legacy) is None


def test_route_mismatch_fails_closed_for_enrolled_only(board, tmp_path, monkeypatch):
    enrolled, legacy, _, _ = configured_policy(board, tmp_path, monkeypatch)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET model_override=NULL WHERE id=?", (enrolled,))
    assert dispatch._factory_policy_guard(board, enrolled) == "factory_route_mismatch"
    assert dispatch._factory_policy_guard(board, legacy) is None


@pytest.mark.parametrize("bad_rollout", [
    {"mode": "task_allowlist", "task_ids": []},
    {"mode": "task_allowlist", "task_ids": "not-a-list"},
    {"mode": "task_allowlist", "task_ids": ["t_one", "t_one"]},
    {"mode": "all", "task_ids": ["t_one"]},
    {"mode": "task_allowlist", "task_ids": ["t_one"], "unexpected": True},
])
def test_invalid_rollout_fails_closed_before_any_legacy_bypass(board, tmp_path, monkeypatch, bad_rollout):
    _, legacy, policy, save = configured_policy(board, tmp_path, monkeypatch)
    policy["rollout"] = bad_rollout
    save()
    assert dispatch._factory_policy_guard(board, legacy) == "factory_policy_invalid"


def test_absent_rollout_preserves_existing_strict_global_behavior(board, tmp_path, monkeypatch):
    _, legacy, policy, save = configured_policy(board, tmp_path, monkeypatch)
    del policy["rollout"]
    save()
    assert dispatch._factory_policy_guard(board, legacy) == "factory_missing_role_adapter"


def test_wrong_board_fails_closed_even_for_unenrolled_task(board, tmp_path, monkeypatch):
    _, legacy, policy, save = configured_policy(board, tmp_path, monkeypatch)
    policy["board_db"] = str((tmp_path / "wrong.sqlite").resolve())
    save()
    assert dispatch._factory_policy_guard(board, legacy) == "factory_policy_invalid"


def test_foreign_process_home_fails_closed_before_allowlist_bypass(board, tmp_path, monkeypatch):
    _, legacy, _, _ = configured_policy(board, tmp_path, monkeypatch)
    foreign_home = tmp_path / "other-operator" / ".hermes"
    foreign_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(foreign_home))
    assert dispatch._factory_policy_guard(board, legacy) == "factory_policy_invalid"


def test_same_owner_named_profile_is_allowed(board, tmp_path, monkeypatch):
    enrolled, _, _, _ = configured_policy(board, tmp_path, monkeypatch)
    profile_home = tmp_path / ".hermes" / "profiles" / "orchestrator"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    assert dispatch._factory_policy_guard(board, enrolled) is None


def test_blocked_independent_reviewer_does_not_freeze_safe_legacy_writer(board, tmp_path, monkeypatch):
    """A capacity-blocked review is a lateral dependency, not a global stop."""
    enrolled, legacy, policy, save = configured_policy(board, tmp_path, monkeypatch)
    policy["task_roles"][enrolled] = {"role": "independent_reviewer"}
    save()
    assert kb.block_task(board, enrolled, kind="capability", reason="provider 429")
    result = dispatch.dispatch_once(board, dry_run=True, max_in_progress=2)
    planned = {item[0] for item in result.spawned}
    assert enrolled not in planned
    assert legacy in planned
    assert not result.respawn_guarded


def test_task_allowlist_dispatches_enrolled_and_legacy_dry_run(board, tmp_path, monkeypatch):
    enrolled, legacy, _, _ = configured_policy(board, tmp_path, monkeypatch)
    result = dispatch.dispatch_once(board, dry_run=True, max_in_progress=2)
    planned = {item[0] for item in result.spawned}
    assert enrolled in planned
    assert legacy in planned
    assert not result.respawn_guarded


def test_claim_revalidates_enrolled_policy_inside_sqlite_transaction(board, tmp_path, monkeypatch):
    """F1: a direct claim cannot bypass an enabled owner/task route policy."""
    enrolled, legacy, _, _ = configured_policy(board, tmp_path, monkeypatch)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET model_override=NULL WHERE id=?", (enrolled,))
    assert kb.claim_task(board, enrolled) is None
    assert kb.get_task(board, enrolled).status == "ready"
    assert kb.claim_task(board, legacy) is not None


def test_claim_rejects_stale_provider_snapshot_even_if_new_route_is_valid(board, tmp_path, monkeypatch):
    """F1: guard A -> route B before claim must NOT bind A's ledger to B's worker."""
    enrolled, _, policy, save = configured_policy(board, tmp_path, monkeypatch)
    guard_context = {}
    assert dispatch._factory_policy_guard(board, enrolled, guard_context) is None
    policy["routes"]["other"] = {
        "profile": "codex-worker", "provider": "minimax", "model": "review-model",
        "eligible_roles": ["read_only"],
    }
    save()
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET provider_override=?, model_override=? WHERE id=?",
            ("minimax", "review-model", enrolled),
        )
    assert dispatch._factory_policy_guard(board, enrolled) is None
    assert kb.claim_task(board, enrolled, expected_factory_context=guard_context) is None
    assert kb.get_task(board, enrolled).status == "ready"


def test_capacity_ledger_stays_in_ashley_owner_home_not_policy_location(board, tmp_path, monkeypatch):
    """F8: a policy outside the home must not redirect provider ledger writes."""
    enrolled, _, _, _ = configured_policy(board, tmp_path, monkeypatch)
    context = {}
    assert dispatch._factory_policy_guard(board, enrolled, context) is None
    assert Path(context["ledger"]).resolve().parent == (tmp_path / ".hermes").resolve()
