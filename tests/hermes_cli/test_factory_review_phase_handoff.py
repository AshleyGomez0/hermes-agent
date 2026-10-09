"""CP47 exact-SHA native same-card review automatically follows the writer."""
import json
from pathlib import Path

import pytest
from hermes_cli import config as config_module
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch

SHA = "abcdef0123456789abcdef0123456789abcdef01"


@pytest.fixture()
def canary(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    kb.init_db()
    with kbc.connect() as conn:
        path = tmp_path / "factory.json"
        board_db = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
        tid = kb.create_task(conn, title="automated review contract",
                             assignee="codex-worker", provider_override="openai-codex",
                             model_override="canary-model")
        policy = {
            "schema_version": 1, "enabled": True,
            "owner_home": str(home.resolve()), "board_db": str(Path(board_db).resolve()),
            "routes": {
                "writer": {"provider": "openai-codex", "profile": "codex-worker",
                           "model": "canary-model", "eligible_roles": ["writer"]},
                "reviewer": {"provider": "openai-codex", "profile": "astra",
                             "model": "canary-model", "eligible_roles": ["independent_reviewer"]},
                "minimax": {"preserved": True},
            },
            "require_independent_reviewer": True,
            "qwen": {"bounded_only": True, "tools": False},
            "deterministic": "scripts",
            "task_roles": {
                tid: {"role": "writer", "review_phase": {
                    "role": "independent_reviewer",
                    "writer_task_id": "self", "sha_source": "writer_handoff"
                }}
            },
            "rollout": {"mode": "task_allowlist", "task_ids": [tid]},
        }
        def save():
            path.write_text(json.dumps(policy), encoding="utf-8")
        save()
        monkeypatch.setattr(config_module, "load_config",
                            lambda: {"kanban": {"factory_routing_policy_path": str(path)}})
        yield conn, tid, policy, save


def _writer_handoff(conn, tid, *, reviewer="astra", metadata=None):
    assert dispatch._factory_policy_guard(conn, tid) is None
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.request_review(conn, tid, reviewer=reviewer, metadata=metadata,
                             summary="ready", expected_run_id=claimed.current_run_id)


def test_full_same_card_review_role_automatically_switches_and_tracks_sha(canary):
    conn, tid, policy, save = canary
    _writer_handoff(conn, tid, metadata={"commit": SHA})
    context = {}
    assert dispatch._factory_policy_guard(conn, tid, context) is None
    assert context["reviewed_sha"] == SHA
    assert policy["enabled"] is True
    assert policy["task_roles"][tid]["role"] == "writer"
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    assert review.assignee == "astra"
    assert kb.complete_task(conn, tid, summary="independent exact sha", expected_run_id=review.current_run_id)
    assert kb.get_task(conn, tid).status == "done"


@pytest.mark.parametrize("metadata", [None, {}, {"commit": "old-sha"}, {"commit": 123}])
def test_review_without_proven_exact_sha_fails_closed(canary, metadata):
    conn, tid, _, _ = canary
    _writer_handoff(conn, tid, metadata=metadata)
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"


def test_self_review_is_rejected_even_with_a_sha(canary):
    conn, tid, policy, save = canary
    policy["routes"]["writer"]["eligible_roles"].append("independent_reviewer")
    save()
    _writer_handoff(conn, tid, reviewer="codex-worker", metadata={"commit": SHA})
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"


def test_tampered_review_event_cannot_bind_sha(canary):
    conn, tid, _, _ = canary
    _writer_handoff(conn, tid, metadata={"commit": SHA})
    conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='review_requested'",
                 ('{"implementer":"astra","reviewer":"astra"}', tid))
    conn.commit()
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"


def test_invalid_review_phase_stays_closed(canary):
    conn, tid, policy, save = canary
    policy["task_roles"][tid]["review_phase"] = {"role": "independent_reviewer",
                                                  "writer_task_id": "self",
                                                  "reviewed_sha": SHA}
    save()
    _writer_handoff(conn, tid, metadata={"commit": SHA})
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"
