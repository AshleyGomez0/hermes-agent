"""R3: reversible, owner-home-only CP47 policy migration preflight."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def staged_inputs(tmp_path, monkeypatch):
    owner = tmp_path / "ashley" / ".hermes"
    owner.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    shared = tmp_path / "factory" / "config"
    shared.mkdir(parents=True)
    source = shared / "factory-routing.json"
    document = {
        "schema_version": 1, "enabled": True, "owner_home": str(owner.resolve()),
        "board_db": str((tmp_path / "factory" / "kanban.db").resolve()),
        "require_independent_reviewer": True,
        "qwen": {"bounded_only": True, "tools": False},
        "deterministic": "scripts",
        "routes": {
            "writer": {"provider": "openai-codex", "model": "model",
                       "profile": "writer", "eligible_roles": ["writer"]},
            "minimax": {"preserved": True},
        },
        "dispatch_profiles": ["writer"],
        "task_owners": {"t_ashley": str(owner.resolve())},
        "task_roles": {"t_ashley": {"role": "writer"}},
        "rollout": {"mode": "task_allowlist", "task_ids": ["t_ashley"]},
    }
    source.write_text(json.dumps(document), encoding="utf-8")
    return owner, shared, source, document


def test_r3_dry_run_and_stage_are_reversible_and_never_activate(staged_inputs):
    from hermes_cli.kanban_factory_policy_stage import stage_policy, rollback_stage
    owner, shared, source, document = staged_inputs
    original = source.read_bytes()
    preview = stage_policy(source, owner, source_root=shared)
    assert preview["ready_for_activation"] is False
    assert not Path(preview["destination"]).exists()
    receipt = stage_policy(source, owner, source_root=shared, dry_run=False)
    dest = Path(receipt["destination"])
    assert dest.is_file() and owner.resolve() in dest.resolve().parents
    assert json.loads(dest.read_text(encoding="utf-8"))["enabled"] is False
    assert source.read_bytes() == original
    assert receipt["source_sha256"] != "" and receipt["staged_sha256"] != ""
    assert stage_policy(source, owner, source_root=shared, dry_run=False) == receipt
    rollback_stage(dest, owner, receipt["staged_sha256"])
    assert not dest.exists() and source.read_bytes() == original


@pytest.mark.parametrize("corruption", ["missing_owner", "foreign_owner", "missing_allowlist"])
def test_r3_incompatible_source_never_staged(staged_inputs, corruption):
    from hermes_cli.kanban_factory_policy_stage import stage_policy
    owner, shared, source, document = staged_inputs
    if corruption == "missing_owner":
        document.pop("owner_home")
    elif corruption == "foreign_owner":
        document["owner_home"] = str(owner.parent / "david")
    else:
        document.pop("task_owners")
    source.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        stage_policy(source, owner, source_root=shared, dry_run=False)
    assert not (owner / "factory" / "cp47").exists()


def test_r3_rejects_source_outside_declared_factory_root(staged_inputs, tmp_path):
    from hermes_cli.kanban_factory_policy_stage import stage_policy
    owner, shared, source, document = staged_inputs
    foreign = tmp_path / "david-policy.json"
    foreign.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        stage_policy(foreign, owner, source_root=shared, dry_run=False)
    assert not (owner / "factory" / "cp47").exists()


def test_r3_rejects_symlink_source(staged_inputs):
    from hermes_cli.kanban_factory_policy_stage import stage_policy
    owner, shared, source, _ = staged_inputs
    symlink = shared / "alias.json"
    try:
        symlink.symlink_to(source)
    except OSError:
        pytest.skip("Windows symlink creation unavailable")
    with pytest.raises(ValueError):
        stage_policy(symlink, owner, source_root=shared, dry_run=False)


def test_r3_rollback_rejects_tampering(staged_inputs):
    from hermes_cli.kanban_factory_policy_stage import stage_policy, rollback_stage
    owner, shared, source, _ = staged_inputs
    receipt = stage_policy(source, owner, source_root=shared, dry_run=False)
    destination = Path(receipt["destination"])
    destination.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(ValueError):
        rollback_stage(destination, owner, receipt["staged_sha256"])
    assert destination.exists()



def test_r3_legacy_disabled_policy_can_be_staged_but_not_activated(staged_inputs):
    from hermes_cli.kanban_factory_policy_stage import stage_policy, rollback_stage
    owner, shared, source, doc = staged_inputs
    doc["enabled"] = False
    doc.pop("rollout")
    doc.pop("task_owners")
    doc.pop("dispatch_profiles")
    source.write_text(json.dumps(doc), encoding="utf-8")
    original = source.read_bytes()
    receipt = stage_policy(
        source, owner, source_root=shared, dry_run=False, allow_incomplete=True)
    staged = Path(receipt["destination"])
    assert receipt["activation_blockers"]
    assert receipt["ready_for_activation"] is False
    assert json.loads(staged.read_text(encoding="utf-8"))["enabled"] is False
    assert source.read_bytes() == original
    rollback_stage(staged, owner, receipt["staged_sha256"])
    assert not staged.exists()


def test_r3_incomplete_migration_cannot_use_david_owner(staged_inputs):
    from hermes_cli.kanban_factory_policy_stage import stage_policy
    owner, shared, source, doc = staged_inputs
    doc["owner_home"] = str(owner.parent / "david" / ".hermes")
    doc.pop("rollout")
    source.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError):
        stage_policy(source, owner, source_root=shared, dry_run=False, allow_incomplete=True)



def test_r3_foreign_parent_symlink_never_creates_a_directory(staged_inputs, tmp_path):
    from hermes_cli.kanban_factory_policy_stage import stage_policy
    owner, shared, source, _ = staged_inputs
    foreign = tmp_path / "david"
    foreign.mkdir()
    try:
        (owner / "factory").symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("Windows directory symlinks unavailable")
    with pytest.raises(ValueError):
        stage_policy(source, owner, source_root=shared, dry_run=False)
    assert not (foreign / "cp47").exists()


def test_r3_rollback_refuses_foreign_parent_junction(staged_inputs, tmp_path):
    import hashlib
    from hermes_cli.kanban_factory_policy_stage import rollback_stage
    owner, _, _, _ = staged_inputs
    foreign = tmp_path / "david"
    (foreign / "cp47").mkdir(parents=True)
    target = foreign / "cp47" / ("factory-routing-" + "a" * 16 + ".staged.json")
    target.write_text('{"enabled":false}', encoding="utf-8")
    try:
        (owner / "factory").symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("Windows directory symlinks unavailable")
    alias = owner / "factory" / "cp47" / target.name
    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        rollback_stage(alias, owner, expected)
    assert target.exists()
