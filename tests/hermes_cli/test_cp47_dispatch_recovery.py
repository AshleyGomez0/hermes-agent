"""CP47 adversarial dispatch tests for exact claimed-run 429 recovery."""

from pathlib import Path

import pytest

from hermes_cli import config as config_module
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


class QuotaWall(RuntimeError):
    status_code = 429


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda profile: True)
    # A profile without a policy must follow the standard dispatch path.
    monkeypatch.setattr(config_module, "load_config", lambda: {"kanban": {}})
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def test_spawn_quota_cannot_resurrect_archived_claim(board):
    tid = kb.create_task(board, title="archive during 429", assignee="codex-worker")

    def spawn(task, workspace):
        assert kb.archive_task(board, task.id)
        raise QuotaWall("test 429")

    kbd.dispatch_once(board, spawn_fn=spawn, max_in_progress=1)
    assert kb.get_task(board, tid).status == "archived"
    assert not [
        e for e in kb.list_events(board, tid) if e.kind == "rate_limited"
    ]


def test_spawn_quota_regates_changed_parent(board):
    parent = kb.create_task(board, title="required predecessor")
    assert kb.complete_task(board, parent, result="done")
    tid = kb.create_task(
        board, title="dependent worker", assignee="codex-worker", parents=[parent],
    )

    def spawn(task, workspace):
        assert kb.archive_task(board, parent)
        raise QuotaWall("test 429")

    kbd.dispatch_once(board, spawn_fn=spawn, max_in_progress=1)
    assert kb.get_task(board, parent).status == "archived"
    assert kb.get_task(board, tid).status == "todo"


def test_spawn_quota_requeues_only_live_claim(board):
    tid = kb.create_task(board, title="quota pressure", assignee="codex-worker")

    def spawn(task, workspace):
        raise QuotaWall("test 429")

    result = kbd.dispatch_once(board, spawn_fn=spawn, max_in_progress=1)
    assert tid in result.rate_limited
    assert kb.get_task(board, tid).status == "ready"
    assert kb.get_task(board, tid).current_run_id is None


def test_archived_parent_purge_releases_only_satisfied_children(board):
    former_parent = kb.create_task(board, title="completed parent")
    assert kb.complete_task(board, former_parent, result="done")
    free_child = kb.create_task(
        board, title="child after purge", assignee="codex-worker",
        parents=[former_parent],
    )
    pending_parent = kb.create_task(board, title="still unfinished parent")
    gated_child = kb.create_task(
        board, title="child with another dependency", assignee="codex-worker",
        parents=[former_parent, pending_parent],
    )
    assert kb.get_task(board, free_child).status == "ready"
    assert kb.get_task(board, gated_child).status == "todo"

    assert kb.archive_task(board, former_parent)
    assert kb.get_task(board, free_child).status == "todo"
    assert kb.delete_archived_task(board, former_parent)
    assert kb.get_task(board, free_child).status == "ready"
    assert kb.get_task(board, gated_child).status == "todo"
    assert kb.complete_task(board, pending_parent, result="done")
    assert kb.get_task(board, gated_child).status == "ready"


def test_routing_policy_uses_config_not_environment(board, monkeypatch, tmp_path):
    poisoned = tmp_path / "poison.json"
    poisoned.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("HERMES_FACTORY_ROUTING_POLICY", str(poisoned))
    tid = kb.create_task(board, title="policy guard", assignee="codex-worker")
    assert kbd._factory_policy_guard(board, tid) is None

    # Explicit opt-in to a missing path is invalid, never silently ignored.
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(
        config_module, "load_config",
        lambda: {"kanban": {"factory_routing_policy_path": str(missing)}},
    )
    assert kbd._factory_policy_guard(board, tid) == "factory_policy_invalid"

    # Explicit disabled policy is accepted through config.yaml.
    policy = tmp_path / "route.json"
    policy.write_bytes(b'\xef\xbb\xbf' + b'{"schema_version": 1, "enabled": false}')
    monkeypatch.setattr(
        config_module, "load_config",
        lambda: {"kanban": {"factory_routing_policy_path": str(policy)}},
    )
    assert kbd._factory_policy_guard(board, tid) is None
