"""Native foreign processes cannot steal unacknowledged Factory admission."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
from tests.test_provider_capacity_dispatch import board
from tests.test_factory_control_scope import bound_review
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch

pytestmark = pytest.mark.platforms("windows")


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("descendant", [False, True])
def test_foreign_process_cannot_register_before_intended_worker(board, monkeypatch, bound, descendant):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert dispatch.dispatch_once(conn, spawn_fn=lambda *args: None).spawned
    run = kb._current_run_id(conn, task)
    if bound:
        # This test process represents the published launcher/dispatcher. Neither
        # of its parallel children nor a child's child is the intended process.
        dispatch._set_worker_pid(conn, task, os.getpid(), expected_run_id=run)
    before = tuple(conn.execute("SELECT worker_pid,worker_started_at FROM task_runs WHERE id=?", (run,)).fetchone())
    env = dict(os.environ, HERMES_KANBAN_TASK=task, HERMES_KANBAN_RUN_ID=str(run),
        HERMES_KANBAN_FACTORY_RUN=f"{task}:{run}", HERMES_KANBAN_DB=str(kb.kanban_db_path()),
        PYTHONPATH=str(Path(dispatch.__file__).resolve().parents[1]), PYTHONUTF8="1")
    env.pop("_HERMES_FACTORY_WORKER_BOOT", None)
    code = ("import json,os;from tools.kanban_tools import register_current_worker_from_env;"
            "print(json.dumps(dict(pid=os.getpid(),accepted=register_current_worker_from_env(worker_session_id='rogue-process'))))")
    if descendant:
        code = ("import subprocess,sys; p=subprocess.run([sys.executable,'-c'," + repr(code) + "],"
                "capture_output=True,text=True,encoding='utf-8',timeout=30);"
                "print(p.stdout,end='');sys.exit(p.returncode)")
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout.strip().splitlines()[-1])
    assert row["pid"] != os.getpid()
    assert row["accepted"] is False
    assert tuple(conn.execute("SELECT worker_pid,worker_started_at FROM task_runs WHERE id=?", (run,)).fetchone()) == before
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='factory_worker_started'", (task,)).fetchone()[0] == 0
