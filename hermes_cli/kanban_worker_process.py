"""Run-scoped process registration across native Windows launcher chains."""
from __future__ import annotations

import json
import os


BOOT_MARKER = "_HERMES_FACTORY_WORKER_BOOT"


def direct_factory_launch(env, project_root):
    """Pin the parent's dependency environment but spawn the actual Python image.

    A venv redirector has a different PID and transferable descendants. Factory
    authority instead belongs to exactly the process returned by Popen.
    """
    import sys
    from pathlib import Path
    from pm.environments import committed_venv

    executable = Path(sys._base_executable).resolve(strict=True)
    environment = (Path(sys.prefix) if sys.prefix != sys.base_prefix
                   else committed_venv(project_root))
    env[BOOT_MARKER] = str(environment) if environment is not None else ""
    return [str(executable), "-m", "hermes_cli.main"]


def bootstrap_direct_factory_worker():
    """Before CLI/plugin imports, require this exact PID's durable spawn receipt.

    A killed dispatcher before PID publication causes a bounded, fail-closed
    exit. Neither an unbound child nor a sibling/descendant may claim the run.
    Reuse PM's lifetime lease and site activation for the parent's venv instead
    of asking its executable redirector to create a second process.
    """
    from pathlib import Path
    import sqlite3
    import sys
    import time

    environment = os.environ.pop(BOOT_MARKER)
    task, run = os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_RUN_ID")
    if not task or not run or os.environ.get("HERMES_KANBAN_FACTORY_RUN") != f"{task}:{run}":
        raise RuntimeError("Factory bootstrap has no exact admitted run")
    database = Path(os.environ["HERMES_KANBAN_DB"]).resolve(strict=True)
    deadline = time.monotonic() + 10
    while True:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute("SELECT t.status,t.current_run_id,r.worker_pid FROM tasks t "
                "JOIN task_runs r ON r.task_id=t.id AND r.id=? WHERE t.id=?", (int(run), task)).fetchone()
        if row is None or row[:2] != ("running", int(run)):
            raise RuntimeError("Factory run changed before bootstrap")
        if row[2] is not None:
            if row[2] != os.getpid():
                raise RuntimeError("Factory spawn belongs to another process")
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Factory dispatcher did not publish this process")
        time.sleep(.05)
    if environment:
        root = Path(environment).resolve(strict=True)
        packages = root / "Lib" / "site-packages"
        if not (root / "pyvenv.cfg").is_file() or not packages.is_dir():
            raise RuntimeError("Factory dependency environment is unavailable")
        # Match the venv's import contract while keeping the real process image.
        sys.prefix = sys.exec_prefix = str(root)
        from hermes_cli.runtime_state import lease_generation
        lease_generation(root)
        import site
        site.addsitedir(str(packages))
        source = str(Path(__file__).resolve().parents[1])
        sys.path[:] = [source, str(packages), *[x for x in sys.path if x not in (source, str(packages))]]
        os.environ["PYTHONPATH"] = os.pathsep.join([source, str(packages)])


def record_spawn(conn, task_id, pid, *, expected_run_id=None):
    """A late dispatcher receipt must not overwrite the worker's own identity."""
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    pid = int(pid)
    fingerprint = dispatch._process_fingerprint(pid) or dispatch.UNVERIFIED_WORKER_FINGERPRINT
    with kb.write_txn(conn):
        task = conn.execute('SELECT status,current_run_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if task is None:
            return
        run_id = expected_run_id if expected_run_id is not None else task['current_run_id']
        if run_id is None or conn.execute('SELECT 1 FROM task_runs WHERE id=? AND task_id=?',
                                         (run_id, task_id)).fetchone() is None:
            return
        registered = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                                  "AND kind='worker_registered' LIMIT 1", (task_id, run_id)).fetchone()
        if task['status'] == 'running' and task['current_run_id'] == run_id and registered is None:
            conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?',
                         (pid, fingerprint, task_id))
            conn.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?',
                         (pid, fingerprint, run_id))
        kb._append_event(conn, task_id, 'spawned', {'pid': pid, 'started_at': fingerprint}, run_id=run_id)


def adopt_worker(conn, task_id, run_id, pid):
    """Seal one interpreter identity without accepting stale runs or sibling processes."""
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    pid, run_id = int(pid), int(run_id)
    fingerprint = dispatch._process_fingerprint(pid) or dispatch.UNVERIFIED_WORKER_FINGERPRINT
    with kb.write_txn(conn):
        row = conn.execute(
            'SELECT t.status,t.current_run_id,t.claim_lock,t.worker_pid,t.worker_started_at,'
            'r.worker_pid AS run_pid,r.worker_started_at AS run_fingerprint FROM tasks t '
            'JOIN task_runs r ON r.task_id=t.id AND r.id=? WHERE t.id=?', (run_id, task_id)).fetchone()
        if row is None or row['status'] != 'running' or row['current_run_id'] != run_id:
            return False
        factory = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind IN ('factory_capacity_permit','factory_review_bound') LIMIT 1", (task_id, run_id)).fetchone()
        if factory is None:
            # Preserve the native non-Factory path: a known launcher PID remains
            # its liveness identity; only an absent local PID is self-registered.
            if row['worker_pid'] is not None or not (row['claim_lock'] or '').startswith(kb._host_prefix()):
                return True
        elif (pid != os.getpid() or row['worker_pid'] != pid
                or fingerprint == dispatch.UNVERIFIED_WORKER_FINGERPRINT):
            return False
        if not (row['claim_lock'] or '').startswith(kb._host_prefix()):
            return False
        if row['worker_pid'] != row['run_pid'] or row['worker_started_at'] != row['run_fingerprint']:
            return False
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                              "AND kind='worker_registered' ORDER BY id", (task_id, run_id)).fetchall()
        identity = {'pid': pid, 'started_at': fingerprint}
        if events:
            try:
                return (len(events) == 1 and json.loads(events[0]['payload']) == identity
                        and row['worker_pid'] == pid and row['worker_started_at'] == fingerprint)
            except (TypeError, ValueError):
                return False
        if row['worker_pid'] is not None and (row['worker_pid'] != pid or row['worker_started_at'] != fingerprint):
            return False
        conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?',
                     (pid, fingerprint, task_id))
        conn.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?',
                     (pid, fingerprint, run_id))
        kb._append_event(conn, task_id, 'worker_registered', identity, run_id=run_id)
    return True
