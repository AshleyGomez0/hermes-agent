"""Real SQLite/PID identity checks; provider transport is replaced, never called."""
import json, os
from test_provider_capacity_dispatch import board
from test_factory_control_scope import bound_review
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.provider_capacity import CapacityBreaker


def started_review(board, monkeypatch, probe=False):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    if probe:
        assert CapacityBreaker(root/'capacity.sqlite',enabled=True).rate_limited(os.path.normcase(str(home.resolve())), 'openai-codex', now=dispatch.time.time()-301)
    assert dispatch.dispatch_once(conn,spawn_fn=lambda *args: None).spawned
    dispatch._set_worker_pid(conn,task,os.getpid())
    run_id=kb._current_run_id(conn,task)
    row=conn.execute('SELECT metadata,worker_started_at FROM task_runs WHERE id=?',(run_id,)).fetchone()
    bound=json.loads(row['metadata'])['factory_review']
    identity={'pid':os.getpid(),'fingerprint':row['worker_started_at'],'session_id':'actual-reviewer-session'}
    # Same immutable event contract as the native startup binder; no model call.
    with kb.write_txn(conn):kb._append_event(conn,task,'factory_worker_started',identity,run_id=run_id)
    return conn,root,task,run_id,bound


def test_review22_rejects_nonwriter_but_forged_session(board,monkeypatch):
    conn,root,task,run_id,bound=started_review(board,monkeypatch)
    metadata={'factory_review':bound,'worker_session_id':'invented-but-different-session'}
    assert kb.complete_task(conn,task,result='untrusted callback',metadata=metadata,expected_run_id=run_id,fire_lifecycle_hook=False) is False
    assert kb.get_task(conn,task).status=='running'


def test_review22_completed_review_probe_closes_after_policy_disabled(board,monkeypatch):
    conn,root,task,run_id,bound=started_review(board,monkeypatch,probe=True)
    metadata={'factory_review':bound,'worker_session_id':'actual-reviewer-session'}
    assert kb.complete_task(conn,task,result='valid callback',metadata=metadata,expected_run_id=run_id,fire_lifecycle_hook=False)
    # Durable admitted permit remains valid even when current routing is off.
    policy_path=root/'routing.json';policy=json.loads(policy_path.read_text());policy['enabled']=False;policy_path.write_text(json.dumps(policy))
    dispatch._factory_recover_successes(conn)
    import sqlite3
    with sqlite3.connect(root/'capacity.sqlite') as ledger:
        assert ledger.execute('SELECT state FROM capacity').fetchone()[0]=='CLOSED'
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE run_id=? AND kind='factory_capacity_observed'",(run_id,)).fetchone()[0]==1
    dispatch._factory_recover_successes(conn)
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE run_id=? AND kind='factory_capacity_observed'",(run_id,)).fetchone()[0]==1


def test_review22_binder_rejects_foreign_pid_and_rebind(board,monkeypatch):
    conn,root,task,run_id,bound=started_review(board,monkeypatch)
    assert kb.bind_factory_worker_identity(conn,task,run_id,'actual-reviewer-session')
    assert kb.bind_factory_worker_identity(conn,task,run_id,'replacement-session') is False
    conn.execute('UPDATE task_runs SET worker_pid=? WHERE id=?',(os.getpid()+999999,run_id));conn.commit()
    assert kb.bind_factory_worker_identity(conn,task,run_id,'actual-reviewer-session') is False
