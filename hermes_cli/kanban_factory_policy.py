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
    return (policy, path) if policy["enabled"] else None


def _validate_owner_and_board(conn, policy):
    owner = policy.get("owner_home")
    board_db = policy.get("board_db")
    if not isinstance(owner, str) or not owner.strip() or not Path(owner).is_absolute():
        raise ValueError("owner_home must be an absolute canonical OAuth root")
    if not isinstance(board_db, str) or not Path(board_db).is_absolute():
        raise ValueError("board_db must be absolute")
    actual = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not actual or Path(actual).resolve() != Path(board_db).resolve():
        raise ValueError("policy board_db differs from this connection")
    if policy.get("require_independent_reviewer") is not True:
        raise ValueError("independent reviewer is required")
    qwen = policy.get("qwen")
    if (not isinstance(qwen, dict) or qwen.get("bounded_only") is not True
            or qwen.get("tools") is not False or policy.get("deterministic") != "scripts"):
        raise ValueError("bounded Qwen / deterministic scripts policy required")
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


def _task_contract(conn, task_id, policy, eligible, owner, board_db, path, context):
    contracts = policy.get("task_roles", {})
    contract = contracts.get(task_id) if isinstance(contracts, dict) else None
    if isinstance(contract, str):
        contract = {"role": contract}
    if not isinstance(contract, dict):
        return "factory_missing_role_adapter"
    role = contract.get("role")
    if role == "independent_reviewer":
        writer = contract.get("writer_task_id")
        sha = contract.get("reviewed_sha")
        if (not isinstance(writer, str) or not writer.strip() or writer == task_id
                or not isinstance(sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None):
            return "factory_invalid_review_contract"
    phase = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if phase is not None and phase["status"] == "review" and role != "independent_reviewer":
        return "factory_invalid_review_contract"
    row = conn.execute(
        "SELECT provider_override, model_override, assignee FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    route = next((r for r in eligible if row is not None and tuple(row) ==
                  (r["provider"], r["model"], r["profile"]) and role in r["eligible_roles"]), None)
    if route is None:
        return "factory_route_mismatch"
    if context is not None:
        context.update(
            owner=os.path.normcase(str(Path(owner).resolve())),
            provider=route["provider"],
            ledger=str(path.resolve().parent / "capacity.sqlite"),
            board=str(Path(board_db).resolve()),
        )
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
        if not _is_task_enrolled(policy, task_id):
            return None
        return _task_contract(conn, task_id, policy, eligible, owner, board_db, path, context)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        _kb._log.warning("kanban: explicit Factory routing policy invalid (%s); dispatch deferred", exc)
        return "factory_policy_invalid"
