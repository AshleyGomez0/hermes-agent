"""Physical Windows subprocess contention, lifetime, ABA and native registration."""
from pathlib import Path
import os,sys,subprocess,json
import pytest
from hermes_cli.factory_workspace_lease import acquire_workspace_lease, WorkspaceLeaseUnavailable
from test_provider_capacity_dispatch import board,policy,card
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch

pytestmark = pytest.mark.platforms("windows")

def child(registry,scope,hold=0):
    import hermes_cli.factory_workspace_lease as m
    code=('import importlib.util,sys,time;from pathlib import Path;'
          's=importlib.util.spec_from_file_location("lease",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
          '\ntry:\n with m.acquire_workspace_lease(Path(sys.argv[2]),Path(sys.argv[3]),task_id="child",run_id=2):\n  print("ACQUIRED",flush=True);time.sleep(float(sys.argv[4]))\n'
          'except m.WorkspaceLeaseUnavailable:\n print("DENIED",flush=True);sys.exit(73)')
    return subprocess.Popen([sys.executable,'-I','-B','-c',code,str(Path(m.__file__).resolve()),str(registry),str(scope),str(hold)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

@pytest.mark.parametrize('alias',['same','upper','descendant','ancestor'])
def test_lease23_real_process_overlap_denied(tmp_path,alias):
    repo=tmp_path/'repo';sub=repo/'sub';sub.mkdir(parents=True);root=tmp_path/'leases'
    held=sub if alias=='ancestor' else repo
    other=repo if alias in ('same','ancestor') else Path(str(repo).upper()) if alias=='upper' else sub
    with acquire_workspace_lease(root,held,task_id='owner',run_id=1):
        p=child(root,other);out,err=p.communicate(timeout=8)
        assert p.returncode==73,(out,err)
    assert not list(root.glob('*.lease'))


def test_lease23_distinct_prefix_not_overlap(tmp_path):
    repo=tmp_path/'repo';other=tmp_path/'repo-other';repo.mkdir();other.mkdir();root=tmp_path/'leases'
    with acquire_workspace_lease(root,repo,task_id='owner',run_id=1):
        p=child(root,other);out,err=p.communicate(timeout=8)
        assert p.returncode==0 and 'ACQUIRED' in out,(out,err)


def test_lease23_aba_replacement_denied_and_crash_releases(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();root=tmp_path/'leases'
    with acquire_workspace_lease(root,repo,task_id='owner',run_id=1) as lease:
        with pytest.raises(OSError):lease.path.rename(lease.path.with_suffix('.foreign'))
        with pytest.raises(OSError):lease.path.write_bytes(b'foreign')
    p=child(root,repo,20)
    try:
        assert p.stdout.readline().strip()=='ACQUIRED'
    finally:
        p.terminate();p.communicate(timeout=8)
    with acquire_workspace_lease(root,repo,task_id='next',run_id=3):pass
    assert not list(root.glob('*.lease'))


def test_lease23_unknown_metadata_never_reclaimed(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();root=tmp_path/'leases';root.mkdir();unknown=root/'unknown.lease';unknown.write_text('{')
    with pytest.raises(WorkspaceLeaseUnavailable):acquire_workspace_lease(root,repo,task_id='owner',run_id=1)
    assert unknown.read_text()=='{'


def test_lease23_native_writer_deferred_without_failure_budget(board,monkeypatch):
    from tools import kanban_tools as kt
    conn,home,root=board
    task=card(conn);policy(board,monkeypatch,task_roles={task:'writer'})
    assert dispatch.dispatch_once(conn,spawn_fn=lambda *a:None).spawned
    run=kb._current_run_id(conn,task)
    workspace=Path(kb.get_task(conn,task).workspace_path)
    with acquire_workspace_lease(home/'runtime/factory-writer-leases',workspace,task_id='other',run_id=999):
        assert kt._claim_factory_workspace_lease(kb,conn,task,run) is False
    row=conn.execute('SELECT status,consecutive_failures FROM tasks WHERE id=?',(task,)).fetchone()
    assert tuple(row)==('ready',0)
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='factory_scope_deferred' AND task_id=?",(task,)).fetchone()[0]==1


def test_lease23_native_writer_holds_past_callback_until_exit(board,monkeypatch):
    from tools import kanban_tools as kt
    conn,home,root=board;task=card(conn);policy(board,monkeypatch,task_roles={task:'writer'})
    assert dispatch.dispatch_once(conn,spawn_fn=lambda *a:None).spawned
    run=kb._current_run_id(conn,task);workspace=Path(kb.get_task(conn,task).workspace_path)
    assert kt._claim_factory_workspace_lease(kb,conn,task,run)
    try:
        assert kt._claim_factory_workspace_lease(kb,conn,task,run)
        assert kb.complete_task(conn,task,result='fixture done',expected_run_id=run,fire_lifecycle_hook=False)
        with pytest.raises(WorkspaceLeaseUnavailable):acquire_workspace_lease(home/'runtime/factory-writer-leases',workspace,task_id='second',run_id=3)
    finally:kt._factory_workspace_leases.pop((task,run)).close()
    with acquire_workspace_lease(home/'runtime/factory-writer-leases',workspace,task_id='second',run_id=3):pass
