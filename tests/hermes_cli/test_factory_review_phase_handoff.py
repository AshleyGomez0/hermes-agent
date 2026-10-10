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
    import subprocess
    import sys
    home = tmp_path / ".hermes"
    home.mkdir()
    checkout = home / "owned-review-checkout"
    checkout.mkdir()
    (checkout / "proof.txt").write_text("real writer checkout", encoding="utf-8")
    def git(*argv):
        return subprocess.run(
            ["git", "-C", str(checkout), *argv],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    git("init", "-q")
    git("add", "proof.txt")
    git("-c", "user.name=CP47 Fixture", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "real fixture commit")
    # Fixture-scoped test SHA must be a real object; the F10 negative cases
    # independently supply fabricated or stale SHA values.
    monkeypatch.setattr(sys.modules[__name__], "SHA", git("rev-parse", "HEAD"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    monkeypatch.setattr(dispatch, "_dispatch_profile_allowlist", lambda normalize: frozenset({"codex-worker", "astra"}))
    kb.init_db()
    with kbc.connect() as conn:
        path = home / "factory.json"
        board_db = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
        tid = kb.create_task(conn, title="automated review contract",
                             assignee="codex-worker", provider_override="openai-codex",
                             model_override="canary-model")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                         (str(checkout.resolve()), tid))
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
            "task_owners": {tid: str(home.resolve())},
            "dispatch_profiles": ["codex-worker", "astra"],
            "task_roles": {
                tid: {"role": "writer", "review_phase": {
                    "role": "independent_reviewer",
                    "writer_task_id": "self", "sha_source": "writer_handoff",
                    "review_checkout": str(checkout.resolve()),
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
    assert kb.complete_task(
        conn, tid, summary="independent exact sha",
        metadata={"reviewed_sha": SHA, "review_verdict": "approved"},
        expected_run_id=review.current_run_id,
    )
    assert kb.get_task(conn, tid).status == "done"


@pytest.mark.parametrize("metadata", [None, {}, {"commit": "old-sha"}, {"commit": 123}])
def test_review_without_proven_exact_sha_fails_closed(canary, metadata):
    conn, tid, _, _ = canary
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata=metadata,
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_self_review_is_rejected_even_with_a_sha(canary):
    conn, tid, policy, save = canary
    policy["routes"]["writer"]["eligible_roles"].append("independent_reviewer")
    save()
    assert dispatch._factory_policy_guard(conn, tid) is None
    claimed = kb.claim_task(conn, tid)
    assert claimed
    assert not kb.request_review(
        conn, tid, reviewer="codex-worker", metadata={"commit": SHA},
        expected_run_id=claimed.current_run_id,
    )
    assert kb.get_task(conn, tid).status == "running"


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
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": SHA},
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"

def test_missing_latest_writer_event_is_not_filled_from_older_run(canary):
    conn, tid, _, _ = canary
    _writer_handoff(conn, tid, metadata={"commit": SHA})
    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,))
    conn.commit()
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"


def test_sha_reader_uses_current_database_assignee_not_caller_claim(canary):
    from hermes_cli.kanban_factory_policy import _review_handoff_sha
    conn, tid, _, _ = canary
    _writer_handoff(conn, tid, metadata={"commit": SHA})
    assert _review_handoff_sha(conn, tid, "someone-else") is None
    assert _review_handoff_sha(conn, tid, "astra") == SHA


def test_cross_provider_review_handoff_swaps_and_restores_overrides(canary):
    """F4: reviewer route and return-to-writer must be atomic with lifecycle."""
    conn, tid, policy, save = canary
    policy["routes"]["minimax"] = {
        "preserved": True, "provider": "minimax", "model": "review-model",
        "profile": "astra", "eligible_roles": ["independent_reviewer"],
    }
    del policy["routes"]["reviewer"]
    save()
    _writer_handoff(conn, tid, metadata={"commit": SHA})
    row = conn.execute(
        "SELECT assignee,provider_override,model_override,status FROM tasks WHERE id=?", (tid,),
    ).fetchone()
    assert tuple(row) == ("astra", "minimax", "review-model", "review")
    ctx = {}
    assert dispatch._factory_policy_guard(conn, tid, ctx) is None
    assert ctx["provider"] == "minimax" and ctx["reviewed_sha"] == SHA
    reviewer = kb.claim_review_task(conn, tid)
    assert reviewer and reviewer.assignee == "astra"
    ok, writer = kb.request_changes(
        conn, tid, reason="legitimate reviewer fixes", expected_run_id=reviewer.current_run_id,
    )
    assert ok and writer == "codex-worker"
    row = conn.execute(
        "SELECT assignee,provider_override,model_override,status FROM tasks WHERE id=?", (tid,),
    ).fetchone()
    assert tuple(row) == ("codex-worker", "openai-codex", "canary-model", "ready")
    assert dispatch._factory_policy_guard(conn, tid) is None


def test_enrolled_writer_cannot_skip_independent_review_with_direct_done(canary):
    """F3: a claimed writer cannot satisfy its own independent-review gate."""
    conn, tid, _, _ = canary
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.complete_task(
        conn, tid, result="writer self-certified", expected_run_id=writer.current_run_id,
    )
    assert kb.get_task(conn, tid).status == "running"


def test_external_review_sha_requires_actual_writer_run_and_commit(canary):
    """F5: a syntactically valid hash and arbitrary writer_task_id are not proof."""
    conn, tid, policy, save = canary
    policy["task_roles"][tid] = {
        "role": "independent_reviewer", "writer_task_id": "t_missing_writer",
        "reviewed_sha": SHA,
    }
    save()
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='review',assignee='astra' WHERE id=?", (tid,),
        )
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"


def test_external_sha_requires_completed_writer_run_and_exact_real_checkout(canary, tmp_path):
    """F5: only exact checkout HEAD with matching writer-run metadata qualifies."""
    import subprocess

    conn, tid, policy, save = canary
    repo = tmp_path / "review-repo"
    repo.mkdir()
    (repo / "README.md").write_text("proof", encoding="utf-8")
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    git("init", "-q")
    git("add", "README.md")
    git("-c", "user.name=CP47 Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "proof")
    commit = git("rev-parse", "HEAD")
    writer = kb.create_task(conn, title="reviewable commit", assignee="codex-worker")
    policy["task_owners"][writer] = policy["owner_home"]
    save()
    claimed = kb.claim_task(conn, writer)
    assert claimed is not None
    assert kb.complete_task(
        conn, writer, result="writer produced commit",
        metadata={"commit": commit}, expected_run_id=claimed.current_run_id,
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(repo), writer))
        conn.execute("UPDATE tasks SET status='review',assignee='astra' WHERE id=?", (tid,))
    policy["task_roles"][tid] = {
        "role": "independent_reviewer", "writer_task_id": writer,
        "reviewed_sha": commit, "review_checkout": str(repo.resolve()),
    }
    save()
    context = {}
    assert dispatch._factory_policy_guard(conn, tid, context) is None
    assert context["reviewed_sha"] == commit
    policy["task_roles"][tid]["reviewed_sha"] = "f" * 40
    save()
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"


# F10 adversarial regression: a syntactically valid SHA is never provenance.
def _f10_git_checkout(canary, *, external_owner=False):
    import subprocess
    conn, tid, policy, save = canary
    home = Path(policy["owner_home"])
    root = home.parent / "unapproved-checkout" if external_owner else home / "approved-checkout"
    root.mkdir(parents=True, exist_ok=True)
    (root / "proof.txt").write_text("writer result", encoding="utf-8")

    def git(*argv):
        result = subprocess.run(
            ["git", "-C", str(root), *argv],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip()

    git("init", "-q")
    git("add", "proof.txt")
    git("-c", "user.name=CP47 Fixture", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "writer")
    commit = git("rev-parse", "HEAD")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path=?, workspace_kind='dir' WHERE id=?",
                     (str(root.resolve()), tid))
    policy["task_roles"][tid]["review_phase"]["review_checkout"] = str(root.resolve())
    save()
    return root, commit, git


def test_f10_invented_sha_without_checkout_denied_at_writer_handoff(canary):
    conn, tid, policy, save = canary
    policy["task_roles"][tid]["review_phase"].pop("review_checkout")
    save()
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": SHA},
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_nonexistent_hex_commit_denied_at_handoff(canary):
    conn, tid, _, _ = canary
    root, actual, _ = _f10_git_checkout(canary)
    nonexistent = "f" * 40 if actual != "f" * 40 else "e" * 40
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": nonexistent},
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_wrong_checkout_denied_at_handoff(canary):
    conn, tid, policy, save = canary
    root, commit, _ = _f10_git_checkout(canary)
    alternate = root.parent / "other-checkout"
    alternate.mkdir()
    policy["task_roles"][tid]["review_phase"]["review_checkout"] = str(alternate.resolve())
    save()
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": commit},
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_head_drift_denied_at_reviewer_claim(canary):
    conn, tid, policy, save = canary
    root, commit, git = _f10_git_checkout(canary)
    _writer_handoff(conn, tid, metadata={"commit": commit})
    (root / "proof.txt").write_text("second commit", encoding="utf-8")
    git("add", "proof.txt")
    git("-c", "user.name=CP47 Fixture", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "changed after handoff")
    assert git("rev-parse", "HEAD") != commit
    assert dispatch._factory_policy_guard(conn, tid) == "factory_invalid_review_contract"
    assert kb.claim_review_task(conn, tid) is None


def test_f10_unapproved_owner_checkout_denied_at_handoff(canary):
    conn, tid, policy, save = canary
    root, commit, _ = _f10_git_checkout(canary, external_owner=True)
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": commit},
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_head_drift_denied_at_final_completion(canary):
    conn, tid, policy, save = canary
    root, commit, git = _f10_git_checkout(canary)
    _writer_handoff(conn, tid, metadata={"commit": commit})
    reviewer = kb.claim_review_task(conn, tid)
    assert reviewer is not None
    (root / "proof.txt").write_text("new head", encoding="utf-8")
    git("add", "proof.txt")
    git("-c", "user.name=CP47 Fixture", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "drift while reviewing")
    assert not kb.complete_task(
        conn, tid, summary="looks good",
        metadata={"reviewed_sha": commit, "review_verdict": "approved"},
        expected_run_id=reviewer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_handoff_requires_explicit_writer_run_provenance(canary):
    conn, tid, _, _ = canary
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": SHA},
        expected_run_id=None)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_dirty_checkout_denied_before_handoff(canary):
    conn, tid, policy, save = canary
    checkout = Path(policy["task_roles"][tid]["review_phase"]["review_checkout"])
    (checkout / "untracked.txt").write_text("not committed", encoding="utf-8")
    writer = kb.claim_task(conn, tid)
    assert writer is not None
    assert not kb.request_review(
        conn, tid, reviewer="astra", metadata={"commit": SHA},
        expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, tid).status == "running"


def test_f10_non_enrolled_historical_task_retains_old_review_flow(canary):
    conn, tid, policy, save = canary
    legacy = kb.create_task(
        conn, title="legacy compatible", assignee="codex-worker",
        provider_override="openai-codex", model_override="canary-model")
    policy["task_owners"][legacy] = policy["owner_home"]
    assert legacy not in policy["rollout"]["task_ids"]
    save()
    writer = kb.claim_task(conn, legacy)
    assert writer is not None
    assert kb.request_review(
        conn, legacy, reviewer="astra", metadata={"commit": "f" * 40},
        summary="historical compatibility", expected_run_id=writer.current_run_id)
    assert kb.get_task(conn, legacy).status == "review"
