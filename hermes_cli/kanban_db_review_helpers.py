"""Review handoff helpers moved out of the oversized Kanban DB facade."""
from typing import Any


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value


def prior_reviewer(conn, task_id):
    """Reviewer recorded by the latest changes_requested event.

    None means first review; False means malformed provenance.
    """
    from hermes_cli import kanban_db as kb
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = kb._latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = kb._json_dict(kb._row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def completion_allowed(conn, task_id, expected_run_id, metadata):
    """Fail closed on enrolled writer completion lacking an independent reviewer run."""
    import re
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_factory_policy as policy

    try:
        loaded = policy._load_policy()
        if loaded is None:
            return True
        cfg, _ = loaded
        policy._validate_owner_and_board(conn, cfg)
        if not policy._is_task_enrolled(cfg, task_id):
            return True
        contract = cfg.get("task_roles", {}).get(task_id)
        if not isinstance(contract, dict):
            return False
        if contract.get("role") not in ("writer", "test_fix"):
            return True
        phase = contract.get("review_phase")
        if not isinstance(phase, dict) or phase.get("role") != "independent_reviewer":
            return False
        if expected_run_id is None or not isinstance(metadata, dict):
            return False
        row = conn.execute(
            "SELECT status,assignee,current_run_id,provider_override,model_override "
            "FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(expected_run_id):
            return False
        reviewer = row["assignee"]
        rr = conn.execute(
            "SELECT profile FROM task_runs WHERE id=? AND task_id=?",
            (expected_run_id, task_id),
        ).fetchone()
        if rr is None or rr["profile"] != reviewer:
            return False
        claim = kb._latest_event(conn, task_id, "claimed", expected_run_id)
        if kb._json_dict(kb._row_get(claim, "payload")).get("source_status") != "review":
            return False
        writer = conn.execute(
            "SELECT id,profile,metadata FROM task_runs "
            "WHERE task_id=? AND outcome='review_requested' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if writer is None or writer["id"] >= expected_run_id or writer["profile"] == reviewer:
            return False
        handoff = kb._latest_event(conn, task_id, "review_requested", writer["id"])
        provenance = kb._json_dict(kb._row_get(handoff, "payload"))
        if provenance.get("implementer") != writer["profile"] or provenance.get("reviewer") != reviewer:
            return False
        writer_meta = kb._json_dict(writer["metadata"])
        sha = writer_meta.get("commit")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", sha) is None:
            return False
        if metadata.get("review_verdict") != "approved" or metadata.get("reviewed_sha") != sha.lower():
            return False
        expected = next(
            (route for route in policy._eligible_routes(cfg)
             if route["profile"] == reviewer and
             "independent_reviewer" in route["eligible_roles"] and
             (row["provider_override"], row["model_override"]) == (route["provider"], route["model"])),
            None,
        )
        return expected is not None
    except (ValueError, TypeError, OSError, KeyError):
        return False


def gate_empty_completion(conn, task_id, *, result, summary):
    """Keep the native evidence gate, without a large Kanban DB facade."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["status"] == "review":
        return
    stored = row["result"]
    if any(value is not None and str(value).strip() for value in (result, summary, stored)):
        return
    with kb.write_txn(conn):
        kb._append_event(
            conn, task_id, "completion_blocked_empty_result",
            {
                "result_preview": kb._first_line(result, 200) or None,
                "summary_preview": kb._first_line(summary, 200) or None,
            },
        )
    raise kb.EmptyCompletionError(task_id)
