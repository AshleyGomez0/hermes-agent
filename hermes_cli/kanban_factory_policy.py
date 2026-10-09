"""Explicit Factory routing policy validation for the Kanban dispatcher.

Legacy dispatch remains enabled for unenrolled tasks only when an
opt-in task_allowlist policy has already passed global validation.
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

from hermes_cli import kanban_db as _kb


def _factory_policy_guard(conn: sqlite3.Connection, task_id: str, context=None) -> Optional[str]:
    """Validate explicit task contracts without mutating assignments or ledger."""
    import json

    # Behavior belongs to profile config.yaml, never a process-local HERMES_* env.
    # No policy path means the existing dispatcher route is unchanged.
    try:
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
        if not policy["enabled"]:
            return None
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
        # Existing enabled policies remain globally strict. During a deliberate
        # rollout an exact task allowlist preserves the legacy dispatch path for
        # unenrolled work on the shared board. This is never inferred from
        # title, assignee or project; only explicit task IDs are enrolled.
        rollout = policy.get("rollout")
        if rollout is not None:
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
            if task_id not in task_ids:
                return None
        contracts = policy.get('task_roles', {})
        contract = contracts.get(task_id) if isinstance(contracts, dict) else None
        if isinstance(contract, str):
            contract = {'role': contract}
        if not isinstance(contract, dict):
            return 'factory_missing_role_adapter'
        role = contract.get('role')
        if role == 'independent_reviewer':
            import re
            writer = contract.get('writer_task_id')
            sha = contract.get('reviewed_sha')
            if not isinstance(writer, str) or not writer.strip() or writer == task_id or not isinstance(sha, str) or re.fullmatch(r'[0-9a-fA-F]{40}', sha) is None:
                return 'factory_invalid_review_contract'
        phase = conn.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
        if phase is not None and phase['status'] == 'review' and role != 'independent_reviewer':
            return 'factory_invalid_review_contract'
        row = conn.execute('SELECT provider_override, model_override, assignee FROM tasks WHERE id=?', (task_id,)).fetchone()
        route = next((r for r in eligible if row is not None and tuple(row) == (r['provider'], r['model'], r['profile']) and role in r['eligible_roles']), None)
        if route is None:
            return 'factory_route_mismatch'
        if context is not None:
            context.update(owner=os.path.normcase(str(Path(owner).resolve())), provider=route['provider'],
                           ledger=str(path.resolve().parent / 'capacity.sqlite'), board=str(Path(board_db).resolve()))
        return None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        _kb._log.warning("kanban: explicit Factory routing policy invalid (%s); dispatch deferred", exc)
        return "factory_policy_invalid"
