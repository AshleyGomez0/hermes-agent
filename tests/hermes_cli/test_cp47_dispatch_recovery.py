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


@pytest.mark.parametrize("outcome", ["completed", "review_requested", "changes_requested"])
def test_terminal_handoffs_close_current_half_open_probe(board, monkeypatch, tmp_path, outcome):
    """A valid capacity probe terminates on review handoff as well as DONE."""
    from hermes_cli.provider_capacity import CapacityBreaker

    owner, provider = "ashley-canary", "openai-codex"
    ledger = tmp_path / ".hermes" / "capacity.sqlite"
    breaker = CapacityBreaker(ledger, enabled=True)
    assert breaker.rate_limited(owner, provider, now=1000)
    permit = breaker.acquire(owner, provider, now=1300)
    assert permit.allowed and permit.probe_token
    tid = kb.create_task(board, title="finite quota success", assignee="codex-worker")
    if outcome == "changes_requested":
        first = kb.claim_task(board, tid)
        assert first is not None
        assert kb.request_review(
            board, tid, summary="implementation ready", reviewer="astra",
            expected_run_id=first.current_run_id,
        )
        claimed = kb.claim_review_task(board, tid)
    else:
        claimed = kb.claim_task(board, tid)
    assert claimed is not None
    run_id = claimed.current_run_id
    dbfile = next(row[2] for row in board.execute("PRAGMA database_list") if row[1] == "main")
    scope = {
        "owner": owner, "provider": provider, "ledger": str(ledger),
        "board": str(Path(dbfile).resolve()), "probe_token": permit.probe_token,
    }
    assert breaker.bind_probe(
        owner, provider, permit.probe_token,
        board=scope["board"], run_id=str(run_id), now=1301,
    )
    with kb.write_txn(board):
        kb._append_event(board, tid, "factory_capacity_permit", scope, run_id=run_id)

    if outcome == "completed":
        assert kb.complete_task(
            board, tid, result="verified", expected_run_id=run_id,
        )
    elif outcome == "review_requested":
        assert kb.request_review(
            board, tid, summary="reviewable", reviewer="astra",
            expected_run_id=run_id,
        )
    else:
        assert kb.request_changes(
            board, tid, reason="changes required", expected_run_id=run_id,
        )[0]
    assert board.execute(
        "SELECT outcome FROM task_runs WHERE id=?", (run_id,),
    ).fetchone()[0] == outcome

    from hermes_cli import kanban_factory_policy as factory_policy

    def recovery_scope(_conn, _task_id, _run_profile, _permit):
        return {key: scope[key] for key in ("owner", "provider", "ledger", "board")}

    monkeypatch.setattr(factory_policy, "_factory_recovery_context", recovery_scope)
    kbd._factory_recover_successes(board)
    assert breaker.acquire(owner, provider, now=1302).state == "CLOSED"
    recorded = [
        e for e in kb.list_events(board, tid)
        if e.kind == "factory_capacity_observed" and e.run_id == run_id
    ]
    assert len(recorded) == 1
    assert recorded[0].payload["closed"] is True
    kbd._factory_recover_successes(board)
    assert len([
        e for e in kb.list_events(board, tid)
        if e.kind == "factory_capacity_observed" and e.run_id == run_id
    ]) == 1


def test_writer_probe_closes_after_valid_cross_provider_review_handoff(board, monkeypatch, tmp_path):
    """The writer's half-open probe belongs to its RUN, not the task's new reviewer."""
    import json
    import os
    from hermes_cli.provider_capacity import CapacityBreaker

    owner = os.path.normcase(str((tmp_path / ".hermes").resolve()))
    provider = "openai-codex"
    model = "writer-model"
    ledger = tmp_path / ".hermes" / "capacity.sqlite"
    policy_path = tmp_path / ".hermes" / "factory-policy.json"
    monkeypatch.setattr(kbd, "_dispatch_profile_allowlist", lambda normalize: frozenset({"codex-worker", "astra"}))
    breaker = CapacityBreaker(ledger, enabled=True)
    assert breaker.rate_limited(owner, provider, now=1000)
    permit = breaker.acquire(owner, provider, now=1300)
    assert permit.allowed and permit.probe_token

    tid = kb.create_task(
        board, title="cross-provider writer completion", assignee="codex-worker",
        provider_override=provider, model_override=model,
    )
    db = next(row[2] for row in board.execute("PRAGMA database_list") if row[1] == "main")
    policy = {
        "schema_version": 1, "enabled": True,
        "owner_home": owner, "board_db": str(Path(db).resolve()),
        "routes": {
            "writer": {"provider": provider, "model": model,
                       "profile": "codex-worker", "eligible_roles": ["writer"]},
            "minimax": {"preserved": True, "provider": "minimax",
                       "model": "reviewer-model", "profile": "astra",
                       "eligible_roles": ["independent_reviewer"]},
        },
        "require_independent_reviewer": True,
        "qwen": {"bounded_only": True, "tools": False}, "deterministic": "scripts",
        "task_owners": {tid: owner},
        "dispatch_profiles": ["codex-worker", "astra"],
        "task_roles": {tid: {"role": "writer", "review_phase": {
            "role": "independent_reviewer", "writer_task_id": "self",
            "sha_source": "writer_handoff",
        }}},
        "rollout": {"mode": "task_allowlist", "task_ids": [tid]},
    }
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(
        config_module, "load_config",
        lambda: {"kanban": {"factory_routing_policy_path": str(policy_path)}},
    )
    assert kbd._factory_policy_guard(board, tid) is None
    claim = kb.claim_task(board, tid)
    assert claim is not None
    run_id = claim.current_run_id
    assert breaker.bind_probe(
        owner, provider, permit.probe_token, board=str(Path(db).resolve()),
        run_id=str(run_id), now=1301,
    )
    scope = {
        "owner": owner, "provider": provider, "ledger": str(ledger),
        "board": str(Path(db).resolve()), "probe_token": permit.probe_token,
    }
    with kb.write_txn(board):
        kb._append_event(board, tid, "factory_capacity_permit", scope, run_id=run_id)
    assert kb.request_review(
        board, tid, reviewer="astra", metadata={"commit": "a" * 40},
        expected_run_id=run_id,
    )
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET provider_override='minimax', model_override='reviewer-model' "
            "WHERE id=?", (tid,),
        )
    assert kbd._factory_policy_guard(board, tid) is None
    from hermes_cli.kanban_factory_policy import _factory_recovery_context
    assert _factory_recovery_context(board, tid, "codex-worker", scope) is not None
    for key, invalid in (
        ("owner", "C:/foreign-operator/.hermes"),
        ("board", str(tmp_path / "other-board.db")),
        ("ledger", str(tmp_path / "other-capacity.sqlite")),
        ("provider", "minimax"),
        ("profile", "astra"),
        ("model", "reviewer-model"),
        ("role", "independent_reviewer"),
    ):
        forged = {**scope, key: invalid}
        assert _factory_recovery_context(board, tid, "codex-worker", forged) is None
    assert _factory_recovery_context(board, tid, "astra", scope) is None
    kbd._factory_recover_successes(board)
    assert breaker.acquire(owner, provider, now=1302).state == "CLOSED"
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='factory_capacity_observed'", (tid, run_id),
    ).fetchone()[0] == 1

def test_unrelated_completed_run_cannot_close_live_writer_probe(board, monkeypatch, tmp_path):
    """A shared board can expose the token; ONLY its bound run may close it."""
    import json
    import os
    from hermes_cli.provider_capacity import CapacityBreaker

    owner = os.path.normcase(str((tmp_path / ".hermes").resolve()))
    provider = "openai-codex"
    ledger = tmp_path / ".hermes" / "capacity.sqlite"
    path = tmp_path / ".hermes" / "factory-policy.json"
    monkeypatch.setattr(kbd, "_dispatch_profile_allowlist", lambda normalize: frozenset({"codex-worker"}))
    db = str(Path(next(r[2] for r in board.execute("PRAGMA database_list") if r[1] == "main")).resolve())
    breaker = CapacityBreaker(ledger, enabled=True)
    assert breaker.rate_limited(owner, provider, now=1000)
    token = breaker.acquire(owner, provider, now=1300).probe_token
    assert token

    active = kb.create_task(board, title="active live quota probe",
        assignee="codex-worker", provider_override=provider, model_override="writer-model")
    unrelated = kb.create_task(board, title="unrelated completed writer",
        assignee="codex-worker", provider_override=provider, model_override="writer-model")
    policy = {
        "schema_version": 1, "enabled": True, "owner_home": owner, "board_db": db,
        "routes": {
            "writer": {"provider": provider, "model": "writer-model", "profile": "codex-worker",
                       "eligible_roles": ["writer", "read_only"]},
            "minimax": {"preserved": True},
        },
        "require_independent_reviewer": True,
        "qwen": {"bounded_only": True, "tools": False}, "deterministic": "scripts",
        "task_owners": {active: owner, unrelated: owner},
        "dispatch_profiles": ["codex-worker"],
        "task_roles": {active: {"role": "writer"}, unrelated: {"role": "read_only"}},
        "rollout": {"mode": "task_allowlist", "task_ids": [active, unrelated]},
    }
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(config_module, "load_config",
        lambda: {"kanban": {"factory_routing_policy_path": str(path)}})
    actual = kb.claim_task(board, active)
    assert actual and actual.current_run_id
    actual_id = actual.current_run_id
    assert breaker.bind_probe(owner, provider, token, board=db,
                              run_id=str(actual_id), now=1301)
    fake = kb.claim_task(board, unrelated)
    assert fake and fake.current_run_id != actual_id
    fake_id = fake.current_run_id
    scope = {"owner": owner, "provider": provider, "ledger": str(ledger),
             "board": db, "probe_token": token,
             "model": "writer-model", "profile": "codex-worker", "role": "read_only"}
    with kb.write_txn(board):
        kb._append_event(board, unrelated, "factory_capacity_permit", scope, run_id=fake_id)
    assert kb.complete_task(board, unrelated, result='unrelated evidence', expected_run_id=fake_id)
    assert board.execute("SELECT ended_at FROM task_runs WHERE id=?", (actual_id,)).fetchone()[0] is None

    kbd._factory_recover_successes(board)
    # The correct state is STILL HALF_OPEN: the bound run is active.
    assert breaker.acquire(owner, provider, now=1302).state == "HALF_OPEN"
    observed = board.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                             "AND kind='factory_capacity_observed'", (unrelated, fake_id)).fetchone()
    assert observed is not None
    assert json.loads(observed[0])["closed"] is False
