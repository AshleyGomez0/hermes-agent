"""Enforce factory worker tool grants at schema AND execution boundaries.

This is intentionally opt-in via dispatcher-provided role context. Workers
without the CP47 role context keep historical toolsets unchanged.
"""
import os
from pathlib import Path

_SAFE_READ = frozenset({
    "read_file", "search_files", "web_search", "web_extract",
    "skills_list", "skill_view", "kanban_show", "kanban_attachments",
})
_LIFECYCLE = frozenset({
    "kanban_request_review", "kanban_complete", "kanban_request_changes",
    "kanban_heartbeat", "kanban_comment", "kanban_block", "kanban_attach",
})
_ROLE_ALLOWED = {
    "writer": _SAFE_READ | _LIFECYCLE | {"write_file", "patch"},
    "test_fix": _SAFE_READ | _LIFECYCLE | {"write_file", "patch"},
    "independent_reviewer": _SAFE_READ | {
        "kanban_complete", "kanban_request_changes", "kanban_heartbeat",
        "kanban_comment", "kanban_block",
    },
    "read_only": _SAFE_READ,
    "qwen_bounded": frozenset(),
}


def safe_cli_toolsets(role):
    """Conservative subprocess launch surface; execution guard remains final."""
    if role not in _ROLE_ALLOWED:
        raise ValueError("unknown CP47 worker role")
    if role == "qwen_bounded":
        return ["search"]  # runtime schema/dispatch still removes every tool
    return ["file", "web", "skills", "kanban"]


def active_role():
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None
    role = os.environ.get("CP47_FACTORY_WORKER_ROLE")
    if role is None:
        return None  # unenrolled legacy workers retain their existing grants
    return role if role in _ROLE_ALLOWED else "qwen_bounded"  # invalid grant fails closed


def select_tools(names):
    role = active_role()
    return set(names) if role is None else set(names) & _ROLE_ALLOWED[role]


def _inside_workspace(path):
    root = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if not root or not isinstance(path, str) or not path.strip():
        return False
    workspace = Path(root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    return resolved != workspace and workspace in resolved.parents


def allowed_call(name, args):
    role = active_role()
    if role is None:
        return True
    if name not in _ROLE_ALLOWED[role]:
        return False
    if name.startswith("kanban_"):
        task_id = args.get("task_id")
        if task_id is not None and task_id != os.environ.get("HERMES_KANBAN_TASK"):
            return False
    if name in {"write_file", "patch"}:
        if name == "patch" and args.get("mode", "replace") != "replace":
            return False
        return _inside_workspace(args.get("path"))
    if name in {"read_file", "search_files"}:
        target = args.get("path") or args.get("file_path") or args.get("directory") or "."
        root = os.environ.get("HERMES_KANBAN_WORKSPACE")
        if not root:
            return False
        workspace = Path(root).resolve()
        entry = Path(target)
        resolved = (entry if entry.is_absolute() else workspace / entry).resolve()
        return resolved == workspace or workspace in resolved.parents
    return True



def grant_for_spawn(task_id, board):
    """Resolve a child-only role pin against the actual SQLite board."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_factory_policy import _factory_policy_guard

    with kbc.connect(board=board) as conn:
        context = {}
        reason = _factory_policy_guard(conn, task_id, context)
        if reason:
            raise ValueError(f"CP47 spawn denied: {reason}")
        if not context:
            return None
        route = context["provider"].casefold() + "/" + context["model"].casefold()
        return "qwen_bounded" if "qwen" in route else context["role"]
