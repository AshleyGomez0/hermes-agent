"""CP47 adversarial tests: actual tool grants, canonical paths and shared owner fencing."""
import json
from pathlib import Path

import pytest

from hermes_cli import config as config_module
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_factory_policy as policy


@pytest.fixture
def cp47(tmp_path, monkeypatch):
    owner = tmp_path / "ashley" / ".hermes"
    owner.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(owner))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    from hermes_cli import config_effective
    monkeypatch.setattr(config_effective, "load_user_config_effective",
                        lambda **kw: {"kanban": {"dispatch_profiles": ["shared", "reviewer"]}})
    kb.init_db()
    with kbc.connect() as conn:
        own = kb.create_task(conn, title="Ashley task", assignee="shared")
        foreign = kb.create_task(conn, title="David task (same profile)", assignee="shared")
        board_db = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
        path = owner / "routing.json"
        doc = {
            "schema_version": 1, "enabled": True,
            "owner_home": str(owner.resolve()), "board_db": str(Path(board_db).resolve()),
            "routes": {
                "writer": {"profile": "shared", "provider": "openai-codex",
                           "model": "writer-model", "eligible_roles": ["writer", "read_only"]},
                "minimax": {"preserved": True},
            },
            "qwen": {"bounded_only": True, "tools": False},
            "deterministic": "scripts", "require_independent_reviewer": True,
            "dispatch_profiles": ["shared", "reviewer"],
            "task_owners": {own: str(owner.resolve())},
            "task_roles": {own: {"role": "read_only"}},
            "rollout": {"mode": "task_allowlist", "task_ids": [own]},
        }
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET provider_override='openai-codex', model_override='writer-model' WHERE id=?", (own,))
        def save():
            path.write_text(json.dumps(doc), encoding="utf-8")
        monkeypatch.setattr(config_module, "load_config",
                            lambda: {"kanban": {"factory_routing_policy_path": str(path)}})
        save()
        yield conn, own, foreign, doc, save, owner, path


def test_f9_foreign_historical_homonym_never_claimed(cp47):
    conn, own, foreign, doc, save, owner, path = cp47
    assert dispatch._factory_policy_guard(conn, own) is None
    assert dispatch._factory_policy_guard(conn, foreign) == "factory_owner_unapproved"
    assert kb.claim_task(conn, foreign) is None
    assert kb.get_task(conn, foreign).status == "ready"
    assert kb.claim_task(conn, own) is not None


def test_f9_foreign_owner_mapping_rejected(cp47):
    conn, own, foreign, doc, save, owner, path = cp47
    doc["task_owners"][foreign] = str(owner.parent / "david" / ".hermes")
    save()
    assert dispatch._factory_policy_guard(conn, foreign) == "factory_owner_unapproved"


def test_f9_approved_legacy_preserves_dispatch(cp47):
    conn, own, foreign, doc, save, owner, path = cp47
    doc["task_owners"][foreign] = str(owner.resolve())
    save()
    assert dispatch._factory_policy_guard(conn, foreign) is None
    assert kb.claim_task(conn, foreign) is not None


def test_f9_missing_dispatch_profile_allowlist_fails_closed(cp47, monkeypatch):
    conn, own, foreign, doc, save, owner, path = cp47
    from hermes_cli import config_effective
    monkeypatch.setattr(config_effective, "load_user_config_effective", lambda **kw: {"kanban": {}})
    assert dispatch._factory_policy_guard(conn, own) == "factory_owner_unapproved"


def test_f8_policy_outside_pinned_owner_root_is_denied(cp47, tmp_path, monkeypatch):
    conn, own, foreign, doc, save, owner, path = cp47
    outside = tmp_path / "unowned" / "routing.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(config_module, "load_config",
                        lambda: {"kanban": {"factory_routing_policy_path": str(outside)}})
    assert dispatch._factory_policy_guard(conn, own) == "factory_policy_invalid"


def test_f8_symlinked_policy_outside_is_denied(cp47, tmp_path, monkeypatch):
    conn, own, foreign, doc, save, owner, path = cp47
    outside = tmp_path / "unowned" / "routing.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps(doc), encoding="utf-8")
    link = owner / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink creation unavailable")
    monkeypatch.setattr(config_module, "load_config",
                        lambda: {"kanban": {"factory_routing_policy_path": str(link)}})
    assert dispatch._factory_policy_guard(conn, own) == "factory_policy_invalid"


def test_f8_symlinked_ledger_is_denied(cp47, tmp_path):
    conn, own, foreign, doc, save, owner, path = cp47
    outside = tmp_path / "unowned-capacity.sqlite"
    outside.touch()
    try:
        (owner / "capacity.sqlite").symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink creation unavailable")
    assert dispatch._factory_policy_guard(conn, own) == "factory_policy_invalid"


@pytest.mark.parametrize("role,forbidden", [
    ("read_only", {"write_file", "patch", "terminal", "execute_code", "kanban_complete"}),
    ("independent_reviewer", {"write_file", "patch", "terminal", "execute_code"}),
    ("qwen_bounded", {"read_file", "write_file", "terminal", "kanban_complete"}),
])
def test_f6_tool_schemas_and_dispatch_denials(monkeypatch, role, forbidden):
    import model_tools
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_cp47test")
    monkeypatch.setenv("CP47_FACTORY_WORKER_ROLE", role)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(Path.cwd()))
    advertised = model_tools._select_tool_names(["hermes-cli"], None, quiet_mode=True)
    assert not advertised.intersection(forbidden)
    for name in forbidden:
        outcome = model_tools.handle_function_call(name, {})
        assert "denied" in str(outcome).lower() or "not authorized" in str(outcome).lower()


def test_f6_writer_file_write_cannot_escape_workspace(tmp_path, monkeypatch):
    import model_tools
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_cp47test")
    monkeypatch.setenv("CP47_FACTORY_WORKER_ROLE", "writer")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "workspace"))
    assert "write_file" in model_tools._select_tool_names(["hermes-cli"], None, True)
    result = model_tools.handle_function_call("write_file", {"path": str(tmp_path / "outside.txt"), "content": "forbidden"})
    assert "denied" in str(result).lower()
    assert not (tmp_path / "outside.txt").exists()



@pytest.mark.parametrize("role", ["read_only", "independent_reviewer", "writer", "qwen_bounded"])
def test_f6_worker_argv_drops_inherited_unsafe_toolsets_and_hooks(monkeypatch, role):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db as kb
    task = kb.Task(
        id="t_cp47_worker_argv", title="role permission", body=None,
        assignee="shared", status="running", priority=0, created_by="test",
        created_at=1, started_at=None, completed_at=None, workspace_kind="scratch",
        workspace_path=None, claim_lock="test", claim_expires=None,
        tenant=None, current_run_id=9,
    )
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets",
                        lambda home: ["terminal", "code_execution", "delegation", "file", "web"])
    args = kbd._worker_argv(task, "shared", None, factory_role=role)
    assert "--accept-hooks" not in args
    assert "--toolsets" in args
    granted = args[args.index("--toolsets") + 1].split(",")
    assert not {"terminal", "code_execution", "delegation", "connections"}.intersection(granted)
    if role == "qwen_bounded":
        assert granted == ["search"]



def test_f6_unknown_role_fails_closed(monkeypatch):
    import model_tools
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_bad_grant")
    monkeypatch.setenv("CP47_FACTORY_WORKER_ROLE", "unknown_privileged")
    assert not model_tools._select_tool_names(["hermes-cli"], None, quiet_mode=True)
    assert "denied" in model_tools.handle_function_call("terminal", {"command": "whoami"}).lower()


def test_f6_writer_cannot_write_through_symlink(tmp_path, monkeypatch):
    import model_tools
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")
    link = root / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink creation unavailable")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_writer")
    monkeypatch.setenv("CP47_FACTORY_WORKER_ROLE", "writer")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(root))
    response = model_tools.handle_function_call("write_file", {"path": str(link), "content": "tampered"})
    assert "denied" in str(response).lower()
    assert outside.read_text(encoding="utf-8") == "unchanged"



def test_f6_spawn_grant_is_read_from_actual_enrolled_board(cp47):
    from hermes_cli.kanban_factory_tool_policy import grant_for_spawn
    conn, own, foreign, *_ = cp47
    assert grant_for_spawn(own, None) == "read_only"
    with pytest.raises(ValueError, match="CP47 spawn denied"):
        grant_for_spawn(foreign, None)


def test_f6_qwen_model_gets_no_tools_even_via_other_provider(cp47):
    from hermes_cli.kanban_factory_tool_policy import grant_for_spawn
    conn, own, _, doc, save, *_ = cp47
    doc["routes"]["writer"]["model"] = "qwen3-coder"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET model_override='qwen3-coder' WHERE id=?", (own,))
    save()
    assert grant_for_spawn(own, None) == "qwen_bounded"



def test_f6_reviewer_cannot_read_foreign_file_or_task(tmp_path, monkeypatch):
    import model_tools
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_ashley")
    monkeypatch.setenv("CP47_FACTORY_WORKER_ROLE", "independent_reviewer")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    denied_read = model_tools.handle_function_call(
        "read_file", {"path": str(tmp_path / "david-secret.txt")})
    denied_task = model_tools.handle_function_call(
        "kanban_show", {"task_id": "t_david"})
    assert "denied" in str(denied_read).lower()
    assert "denied" in str(denied_task).lower()
    assert "kanban_list" not in model_tools._select_tool_names(["kanban"], None, True)
