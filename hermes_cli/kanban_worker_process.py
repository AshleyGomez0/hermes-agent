"""Run-scoped process registration across native Windows launcher chains."""
from __future__ import annotations

import json
import os


def _verified_launcher_ancestor(dispatch, launch_pid, launch_fingerprint, worker_pid):
    """An executing worker may replace only its still-identical Windows ancestor.

    This is process provenance, not an argv substring or an environment assertion.
    An already registered run is checked separately and can never transfer again.
    """
    if os.name != 'nt' or worker_pid != os.getpid():
        return False
    if not launch_fingerprint or launch_fingerprint == dispatch.UNVERIFIED_WORKER_FINGERPRINT:
        return False
    import psutil
    try:
        if dispatch._process_fingerprint(launch_pid) != launch_fingerprint:
            return False
        worker = psutil.Process(worker_pid)
        for parent in worker.parents()[:16]:
            if parent.pid == launch_pid:
                return (parent.is_running() and worker.is_running()
                        and dispatch._process_fingerprint(launch_pid) == launch_fingerprint)
    except (psutil.Error, OSError, ValueError):
        return False
    return False


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
        old_pid = row['worker_pid']
        if old_pid is not None:
            if old_pid == pid:
                if row['worker_started_at'] != fingerprint:
                    return False
            elif not _verified_launcher_ancestor(dispatch, old_pid, row['worker_started_at'], pid):
                return False
        conn.execute('UPDATE tasks SET worker_pid=?,worker_started_at=? WHERE id=?',
                     (pid, fingerprint, task_id))
        conn.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?',
                     (pid, fingerprint, run_id))
        kb._append_event(conn, task_id, 'worker_registered', identity, run_id=run_id)
    return True
