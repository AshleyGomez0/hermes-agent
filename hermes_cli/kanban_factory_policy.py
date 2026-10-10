"""Explicit Factory routing policy validation for the Kanban dispatcher.

An opt-in task allowlist preserves legacy dispatch for unenrolled work only
after the shared policy, owner, board and provider declarations pass validation.
"""

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from hermes_constants import get_process_hermes_home
from hermes_cli import kanban_db as _kb


def _load_policy():
    """Return an explicitly enabled policy and its path, or None."""
    from hermes_cli.config import load_config

    kanban_cfg = (load_config() or {}).get("kanban") or {}
    explicit = kanban_cfg.get("factory_routing_policy_path", "")
    if explicit in ("", None):
        return None
    if not isinstance(explicit, str) or not Path(explicit).is_absolute():
        raise ValueError("factory_routing_policy_path must be an absolute path")
    path = Path(explicit)
    if not path.is_file():
        raise ValueError("configured Factory routing policy is missing")
    policy = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(policy, dict) or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
        raise ValueError("expected schema_version=1")
    if type(policy.get("enabled")) is not bool:
        raise ValueError("enabled must be boolean")
    if policy["enabled"]:
        # Resolve junctions, symlinks and case-normalised path aliases. Owner
        # identity is revalidated against the process in _validate_owner_and_board.
        owner = policy.get("owner_home")
        if not isinstance(owner, str) or not Path(owner).is_absolute():
            raise ValueError("policy lacks canonical owner_home")
        root = Path(owner).resolve()
        resolved = path.resolve()
        if root not in resolved.parents or resolved == root:
            raise ValueError("Factory policy escapes owner home")
        if path.is_symlink():
            raise ValueError("Factory policy is a symlink")
    return (policy, path) if policy["enabled"] else None


def _validate_owner_and_board(conn, policy):
    owner = policy.get("owner_home")
    board_db = policy.get("board_db")
    if not isinstance(owner, str) or not owner.strip() or not Path(owner).is_absolute():
        raise ValueError("owner_home must be an absolute canonical OAuth root")
    if not isinstance(board_db, str) or not Path(board_db).is_absolute():
        raise ValueError("board_db must be absolute")
    active_home = get_process_hermes_home().resolve()
    declared_owner = os.path.normcase(str(Path(owner).resolve()))
    active_root = (active_home.parent.parent
                   if active_home.parent.name.casefold() == "profiles" else active_home)
    if declared_owner != os.path.normcase(str(active_root)):
        raise ValueError("owner_home differs from active Hermes process identity")
    actual = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not actual or Path(actual).resolve() != Path(board_db).resolve():
        raise ValueError("policy board_db differs from this connection")
    if policy.get("require_independent_reviewer") is not True:
        raise ValueError("independent reviewer is required")
    qwen = policy.get("qwen")
    if (not isinstance(qwen, dict) or qwen.get("bounded_only") is not True
            or qwen.get("tools") is not False or policy.get("deterministic") != "scripts"):
        raise ValueError("bounded Qwen / deterministic scripts policy required")
    # A shared board has no implicit tenant ownership for legacy cards.
    # Explicit per-task declarations and per-home dispatch profile allowlists
    # are mandatory while task-scoped CP47 policy is enabled.
    owners = policy.get("task_owners")
    profiles = policy.get("dispatch_profiles")
    if (not isinstance(owners, dict) or not isinstance(profiles, list) or
            not profiles or any(not isinstance(p, str) or not p.strip() for p in profiles)):
        raise ValueError("Factory policy requires task_owners and dispatch_profiles")
    _owner_capacity_ledger(owner)
    return owner, board_db


def _eligible_routes(policy):
    routes = policy.get("routes")
    if not isinstance(routes, dict) or not routes or routes.get("minimax", {}).get("preserved") is not True:
        raise ValueError("routes must preserve MiniMax")
    eligible = []
    for route in routes.values():
        if not isinstance(route, dict):
            raise ValueError("route must be an object")
        if "provider" not in route:
            continue
        if any(not isinstance(route.get(k), str) or not route[k].strip() for k in ("provider", "model", "profile")):
            raise ValueError("explicit route requires profile/provider/model")
        roles = route.get("eligible_roles")
        if not isinstance(roles, list) or not roles or any(r not in ("writer", "read_only", "test_fix", "independent_reviewer") for r in roles):
            raise ValueError("invalid eligible_roles")
        eligible.append(route)
    return eligible


def _is_task_enrolled(policy, task_id):
    """No rollout means historic global strictness, never implicit enrollment."""
    rollout = policy.get("rollout")
    if rollout is None:
        return True
    if not isinstance(rollout, dict) or set(rollout) != {"mode", "task_ids"}:
        raise ValueError("rollout needs exactly mode and task_ids")
    if rollout.get("mode") != "task_allowlist":
        raise ValueError("unknown Factory rollout mode")
    task_ids = rollout.get("task_ids")
    if (not isinstance(task_ids, list) or not task_ids or len(task_ids) > 128
            or any(not isinstance(tid, str) or not tid.startswith("t_")
                   or len(tid) > 96 for tid in task_ids)
            or len(task_ids) != len(set(task_ids))):
        raise ValueError("rollout task_ids must be unique, bounded and non-empty")
    return task_id in task_ids


def _review_handoff_sha(conn, task_id, reviewer, policy=None):
    """Bind an ended writer run to a real owner-approved exact Git HEAD.

    Called during the review-claim transaction; final completion rechecks.
    """
    from hermes_cli.kanban_factory_git_provenance import checkout_commit_matches

    if policy is None:
        loaded = _load_policy()
        if loaded is None:
            return None
        policy, _ = loaded
        _validate_owner_and_board(conn, policy)
    row = conn.execute(
        "WITH latest AS (SELECT id, profile, metadata, ended_at FROM task_runs "
        "WHERE task_id=? AND outcome='review_requested' "
        "ORDER BY id DESC LIMIT 1) "
        "SELECT latest.id AS writer_run_id, latest.profile, latest.metadata, "
        "latest.ended_at, "
        "(SELECT payload FROM task_events WHERE task_id=? "
        "AND kind='review_requested' AND run_id=latest.id "
        "ORDER BY id DESC LIMIT 1) AS payload "
        "FROM latest JOIN tasks ON tasks.id=? "
        "WHERE tasks.status='review' AND tasks.assignee=?",
        (task_id, task_id, task_id, reviewer),
    ).fetchone()
    if (row is None or not row["payload"] or row["ended_at"] is None
            or not isinstance(row["profile"], str)
            or not row["profile"].strip() or row["profile"] == reviewer):
        return None
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='claimed' LIMIT 1", (task_id, row["writer_run_id"]),
    ).fetchone() is None:
        return None
    try:
        provenance = json.loads(row["payload"])
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError):
        _kb._log.warning("kanban: malformed same-card review provenance for task %s", task_id)
        return None
    if (not isinstance(provenance, dict) or not isinstance(metadata, dict)
            or provenance.get("implementer") != row["profile"]
            or provenance.get("reviewer") != reviewer):
        return None
    sha = metadata.get("commit")
    phase = policy.get("task_roles", {}).get(task_id, {}).get("review_phase")
    if not isinstance(sha, str) or not checkout_commit_matches(
            conn, task_id, sha.lower(), phase, policy):
        return None
    return sha.lower()


def _verified_external_writer(conn, writer, sha, reviewer, contract):
    """Require an actual completed writer run and an owner-declared exact checkout."""
    import subprocess
    checkout = contract.get("review_checkout")
    if (not isinstance(checkout, str) or not Path(checkout).is_absolute()
            or not isinstance(writer, str) or not writer.startswith("t_")):
        return False
    row = conn.execute(
        "SELECT status,workspace_path FROM tasks WHERE id=?", (writer,),
    ).fetchone()
    if row is None or row["status"] != "done" or not row["workspace_path"]:
        return False
    root = Path(checkout).resolve()
    if not root.is_dir() or root != Path(row["workspace_path"]).resolve():
        return False
    run = conn.execute(
        "SELECT profile,metadata FROM task_runs "
        "WHERE task_id=? AND outcome='completed' AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (writer,),
    ).fetchone()
    if run is None or not run["profile"] or run["profile"] == reviewer:
        return False
    try:
        data = json.loads(run["metadata"] or "{}")
        if not isinstance(data, dict) or data.get("commit") != sha:
            return False
        check = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        return check.returncode == 0 and check.stdout.strip().lower() == sha
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return False


def _review_sha(conn, task_id, contract, reviewer, policy=None):
    """Validate explicit external review or verified same-card writer handoff."""
    writer = contract.get("writer_task_id")
    if writer == "self":
        if (contract.get("sha_source") != "writer_handoff"
                or "reviewed_sha" in contract):
            return None
        return _review_handoff_sha(conn, task_id, reviewer, policy=policy)
    sha = contract.get("reviewed_sha")
    if (not isinstance(writer, str) or not writer.strip()
            or writer == task_id or contract.get("sha_source") is not None
            or not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None):
        return None
    return sha.lower() if _verified_external_writer(conn, writer, sha.lower(), reviewer, contract) else None


def _owner_capacity_ledger(owner):
    """Keep the mutable capacity ledger inside the validated owner home."""
    root = Path(owner).resolve()
    ledger = root / "capacity.sqlite"
    if ledger.is_symlink() or (ledger.exists() and ledger.resolve().parent != root):
        raise ValueError("Factory capacity ledger escapes owner home")
    return str(ledger)


def _task_contract(conn, task_id, policy, eligible, owner, board_db, path, context):
    contracts = policy.get("task_roles", {})
    contract = contracts.get(task_id) if isinstance(contracts, dict) else None
    if isinstance(contract, str):
        contract = {"role": contract}
    if not isinstance(contract, dict):
        return "factory_missing_role_adapter"
    row = conn.execute(
        "SELECT status, provider_override, model_override, assignee "
        "FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None:
        return "factory_route_mismatch"
    if row["status"] == "review" and "review_phase" in contract:
        contract = contract["review_phase"]
        if not isinstance(contract, dict):
            return "factory_invalid_review_contract"
    role = contract.get("role")
    if row["status"] == "review" and role != "independent_reviewer":
        return "factory_invalid_review_contract"
    sha = None
    if role == "independent_reviewer":
        sha = _review_sha(conn, task_id, contract, row["assignee"], policy=policy)
        if sha is None:
            return "factory_invalid_review_contract"
    route = next((
        route for route in eligible
        if (row["provider_override"], row["model_override"], row["assignee"])
        == (route["provider"], route["model"], route["profile"])
        and role in route["eligible_roles"]
    ), None)
    if route is None:
        return "factory_route_mismatch"
    if context is not None:
        context.update(
            owner=os.path.normcase(str(Path(owner).resolve())),
            provider=route["provider"],
            model=route["model"],
            profile=route["profile"],
            role=role,
            ledger=_owner_capacity_ledger(owner),
            board=str(Path(board_db).resolve()),
        )
        if sha is not None:
            context["reviewed_sha"] = sha
    return None


def _factory_recovery_context(conn, task_id, run_profile, permit):
    """Revalidate an admission's scope without reading the task's NEW review route.

    The task may have switched assignee/provider after writer handoff.
    The immutable run-keyed permit, not current task overrides, attributes
    the writer's successful half-open capacity probe.
    """
    try:
        if not isinstance(permit, dict) or not isinstance(run_profile, str) or not run_profile:
            return None
        loaded = _load_policy()
        if loaded is None:
            return None
        policy, path = loaded
        owner, board_db = _validate_owner_and_board(conn, policy)
        eligible = _eligible_routes(policy)
        if not _is_task_enrolled(policy, task_id):
            return None
        contracts = policy.get("task_roles")
        if not isinstance(contracts, dict) or task_id not in contracts:
            return None
        route = next((
            r for r in eligible
            if r["profile"] == run_profile and r["provider"] == permit.get("provider")
            and permit.get("model", r["model"]) == r["model"]
            and permit.get("profile", run_profile) == run_profile
            and (permit.get("role") is None or permit["role"] in r["eligible_roles"])
        ), None)
        if route is None:
            return None
        expected = {
            "owner": os.path.normcase(str(Path(owner).resolve())),
            "board": str(Path(board_db).resolve()),
            "ledger": _owner_capacity_ledger(owner),
            "provider": route["provider"],
        }
        return expected if all(permit.get(k) == v for k, v in expected.items()) else None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        _kb._log.warning("kanban: invalid Factory capacity recovery scope (%s)", exc)
        return None


def _factory_policy_guard(conn: sqlite3.Connection, task_id: str, context=None) -> Optional[str]:
    """Validate the configured policy without mutating assignments or ledger."""
    try:
        loaded = _load_policy()
        if loaded is None:
            return None
        policy, path = loaded
        owner, board_db = _validate_owner_and_board(conn, policy)
        eligible = _eligible_routes(policy)
        row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or not isinstance(row["assignee"], str):
            return "factory_owner_unapproved"
        task_owners = policy["task_owners"]
        declared = task_owners.get(task_id)
        if (not isinstance(declared, str) or not Path(declared).is_absolute()
                or os.path.normcase(str(Path(declared).resolve())) != os.path.normcase(str(Path(owner).resolve()))
                or row["assignee"] not in policy["dispatch_profiles"]):
            return "factory_owner_unapproved"
        from hermes_cli.kanban_db_dispatch import _dispatch_profile_allowlist
        from hermes_cli.profiles import normalize_profile_name
        allowlist = _dispatch_profile_allowlist(normalize_profile_name)
        if allowlist is None or row["assignee"] not in allowlist:
            return "factory_owner_unapproved"
        if not _is_task_enrolled(policy, task_id):
            return None
        return _task_contract(conn, task_id, policy, eligible, owner, board_db, path, context)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        _kb._log.warning("kanban: explicit Factory routing policy invalid (%s); dispatch deferred", exc)
        return "factory_policy_invalid"
