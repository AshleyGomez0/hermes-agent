"""Native durable hold for repeated provider quota exits, separate from failures."""
from __future__ import annotations

import sqlite3


DEFAULT_QUOTA_HOLD_AFTER = 3
_OUTCOMES = ("rate_limited", "unblocked", "completed", "review_requested",
             "changes_requested", "crashed", "protocol_violation", "gave_up", "blocked")


def hold_if_exhausted(
    conn: sqlite3.Connection, task_id: str, *,
    source_status: str, run_id: int | None,
) -> bool:
    """Under the reaper transaction, park repeated exit-75 tasks for native unblock.

    A successful or non-quota outcome, and especially an explicit native unblock,
    resets the consecutive quota history. Never count quota against task failures.
    """
    from hermes_cli import kanban_db as kb

    threshold = kb._env_int("HERMES_KANBAN_QUOTA_HOLD_AFTER", DEFAULT_QUOTA_HOLD_AFTER)
    if threshold <= 0:
        return False
    threshold = min(threshold, 16)
    marks = ",".join("?" for _ in _OUTCOMES)
    events = conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? AND kind IN (" + marks + ") "
        "ORDER BY id DESC LIMIT ?", (task_id, *_OUTCOMES, threshold),
    ).fetchall()
    if len(events) < threshold or any(r["kind"] != "rate_limited" for r in events):
        return False
    task = conn.execute(
        "SELECT status, block_kind, block_recurrences, current_run_id FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if task is None or task["status"] not in ("ready", "review", "todo"):
        return False
    if task["current_run_id"] is not None:
        return False
    reason = (
        f"provider quota: {threshold} consecutive exit 75 runs; "
        "verify provider capacity before native unblock"
    )
    status, kind, set_sql, vals, payload = kb._route_block(
        "capability", reason, source_status,
        prev_kind=task["block_kind"],
        prev_recurrences=int(task["block_recurrences"] or 0),
    )
    count = conn.execute(
        "UPDATE tasks SET status=?, " + set_sql +
        " WHERE id=? AND status=? AND current_run_id IS NULL",
        (status, *vals, task_id, task["status"]),
    ).rowcount
    if count != 1:
        return False
    payload["quota_hold_after"] = threshold
    kb._append_event(conn, task_id, kind, payload, run_id=run_id)
    return True
