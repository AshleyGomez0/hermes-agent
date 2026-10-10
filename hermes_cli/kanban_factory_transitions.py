"""Enrolled Factory writer/reviewer route transitions, shared-board fail closed.

No policy and unenrolled legacy cards retain their native Kanban lifecycle.
Every enrolled transition is validated while its caller holds the SQLite
write transaction; no task metadata is changed by this helper.
"""
from hermes_cli import kanban_factory_policy as policy


def review_routes(conn, task_id, implementer, reviewer, *, returning=False, origin=None):
    loaded = policy._load_policy()
    if loaded is None:
        return None
    document, _ = loaded
    policy._validate_owner_and_board(conn, document)
    routes = policy._eligible_routes(document)
    if not policy._is_task_enrolled(document, task_id):
        return None
    contract = document.get("task_roles", {}).get(task_id)
    if not isinstance(contract, dict) or contract.get("role") != "writer":
        raise ValueError("enrolled review requires writer role contract")
    phase = contract.get("review_phase")
    if not isinstance(phase, dict) or phase.get("role") != "independent_reviewer":
        raise ValueError("enrolled writer requires independent review phase")
    if not isinstance(implementer, str) or not implementer.strip():
        raise ValueError("missing original writer")
    if not isinstance(reviewer, str) or not reviewer.strip() or reviewer == implementer:
        raise ValueError("reviewer cannot be writer")
    writer_routes = [
        route for route in routes
        if route["profile"] == implementer and "writer" in route["eligible_roles"]
    ]
    reviewer_routes = [
        route for route in routes
        if route["profile"] == reviewer and "independent_reviewer" in route["eligible_roles"]
    ]
    if len(writer_routes) != 1 or len(reviewer_routes) != 1:
        raise ValueError("review writer/reviewer routes must be unambiguous")
    writer, review = writer_routes[0], reviewer_routes[0]
    row = conn.execute(
        "SELECT assignee, provider_override, model_override, status FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise ValueError("task missing at review route transition")
    expected = review if returning else writer
    valid_status = "running" if returning else ("ready", "running")
    if (row["status"] not in ((valid_status,) if isinstance(valid_status, str) else valid_status)
            or (row["assignee"], row["provider_override"], row["model_override"])
            != (expected["profile"], expected["provider"], expected["model"])):
        raise ValueError("task route/status changed before protected handoff")
    if returning and (
        not isinstance(origin, dict)
        or origin.get("writer_route") != {
            "provider": writer["provider"], "model": writer["model"],
        }
    ):
        raise ValueError("review handoff lacks original writer route provenance")
    return {"writer": (writer["provider"], writer["model"]),
            "reviewer": (review["provider"], review["model"])}


def claim_allowed(conn, task_id, expected=None):
    """Inside BEGIN IMMEDIATE: recheck routing and stale dispatcher admission."""
    actual = {}
    if policy._factory_policy_guard(conn, task_id, actual) is not None:
        return False
    if expected is None:
        return True
    if not isinstance(expected, dict):
        return False
    return all(actual.get(key) == value for key, value in expected.items()
               if key not in ("probe_token", "generation"))
