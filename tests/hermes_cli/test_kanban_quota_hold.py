"""Bounded, durable exit-75 quota hold on the native Kanban board."""
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home

def _exited_status(code):
    return code << 8

@pytest.mark.parametrize("lane", ["ready", "review"])
def test_repeated_quota_exits_hold_capability_until_native_unblock(kanban_home, monkeypatch, lane):
    """Three native 75 exits are neutral failures but cause a sticky capacity hold."""
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_QUOTA_HOLD_AFTER", raising=False)
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    with kbc.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="three consecutive 429", assignee="a")
        if lane == "review":
            first = kb.claim_task(conn, tid, claimer=f"{host}:submit")
            assert kb.request_review(conn, tid, summary="review this change", reviewer="reviewer",
                                     expected_run_id=first.current_run_id)
        for i in range(3):
            claimed = (kb.claim_task(conn, tid, claimer=f"{host}:quota{i}")
                       if lane == "ready" else
                       kb.claim_review_task(conn, tid, claimer=f"{host}:review{i}"))
            assert claimed is not None
            pid = 73000+i
            conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
            conn.commit()
            _kbd._record_worker_exit(pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE))
            assert tid not in kbd.detect_crashed_workers(conn)
            task = kb.get_task(conn, tid)
            assert task.consecutive_failures == 0
            assert task.status == ("blocked" if i == 2 else lane)
        assert task.block_kind == "capability"
        assert tid in getattr(_kbd.detect_crashed_workers, "_last_auto_blocked", [])
        rec = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
            "ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert json.loads(rec["payload"])["quota_hold_after"] == 3
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == lane
        # A verified operator wakeup starts a FRESH quota budget.
        claimed = (kb.claim_task(conn, tid, claimer=f"{host}:verified")
                   if lane == "ready" else
                   kb.claim_review_task(conn, tid, claimer=f"{host}:review-verified"))
        assert claimed is not None
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (74000, tid))
        conn.commit()
        _kbd._record_worker_exit(74000, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE))
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).status == lane
