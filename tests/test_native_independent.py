"""Independent offline temp-board review: fake PIDs are not live canaries."""
import sys, json, os, sqlite3
from pathlib import Path
import pytest
from tests.test_provider_capacity_dispatch import board, policy, card, dispatch, kb, kbc, completed_writer_contract
from hermes_cli.provider_capacity import CapacityBreaker

@pytest.mark.parametrize('lane', ['ready','review'])
@pytest.mark.parametrize('evidence', ['live','dead','terminal','unavailable','pending'])
def test_two_real_temp_boards_one_ledger(board, monkeypatch, lane, evidence):
    aconn, owner_home, root = board
    adb=Path(aconn.execute('PRAGMA database_list').fetchone()[2])
    taska=card(aconn)
    contract=lambda connection: completed_writer_contract((connection,owner_home,root)) if lane=='review' else {'role':'writer'}
    with kb.write_txn(aconn):
        aconn.execute('UPDATE tasks SET status=? WHERE id=?',(lane,taska))
    monkeypatch.setattr(dispatch,'review_dispatch_enabled',lambda:True)
    monkeypatch.setattr(dispatch,'check_respawn_guard',lambda *args,**kw:None)
    monkeypatch.setattr(dispatch,'_process_fingerprint',lambda pid: 'offline-A-fingerprint' if pid==987654 else 'offline-B-fingerprint' if pid==987655 else None)
    now=dispatch.time.time()
    monkeypatch.setattr(dispatch.time,'time',lambda:now)
    bhome=root/'board-b-home'
    bhome.mkdir()
    bdb=bhome/'kanban.db'
    monkeypatch.setenv('HERMES_KANBAN_HOME',str(bhome))
    kb.init_db()
    with kbc.connect() as bconn:
        assert Path(bconn.execute('PRAGMA database_list').fetchone()[2]) != adb
        taskb=card(bconn)
        with kb.write_txn(bconn):
            bconn.execute('UPDATE tasks SET status=? WHERE id=?',(lane,taskb))
        monkeypatch.setenv('HERMES_KANBAN_HOME',str(owner_home))
        policy(board,monkeypatch,board_db=str(adb),task_roles={taska:contract(aconn)})
        breaker=CapacityBreaker(root/'capacity.sqlite',enabled=True)
        owner=os.path.normcase(str(owner_home.resolve()))
        assert breaker.rate_limited(owner,'openai-codex',now=now-301)
        acalls=[]
        def aspawn(task,workspace):
            with sqlite3.connect(root/'capacity.sqlite') as ledger:
                binding=ledger.execute('SELECT board,run_id FROM capacity_probe_runs').fetchone()
            run=aconn.execute('SELECT current_run_id FROM tasks WHERE id=?',(task.id,)).fetchone()[0]
            assert binding==(str(adb.resolve()),str(run)), 'spawn preceded durable binding'
            acalls.append(task.id)
            return 987654
        assert dispatch.dispatch_once(aconn,spawn_fn=aspawn).spawned
        assert acalls==[taska]
        runa=aconn.execute('SELECT current_run_id FROM tasks WHERE id=?',(taska,)).fetchone()[0]
        seen=[]
        def identified_offline_liveness(pid,fingerprint):
            seen.append((pid,fingerprint))
            if pid==987654:
                assert fingerprint=='offline-A-fingerprint'
                return evidence=='live'
            if pid==987655:
                assert fingerprint=='offline-B-fingerprint'
                return True
            raise AssertionError('unexpected PID identity')
        monkeypatch.setattr(dispatch,'_worker_alive',identified_offline_liveness)
        if evidence=='terminal':
            with kb.write_txn(aconn):
                aconn.execute("UPDATE task_runs SET worker_pid=NULL,worker_started_at=NULL,ended_at=?,status='done',outcome='spawn_failed' WHERE id=?",(now,runa))
                aconn.execute("UPDATE tasks SET status='done',worker_pid=NULL,claim_lock=NULL WHERE id=?",(taska,))
        elif evidence=='pending':
            with kb.write_txn(aconn):
                aconn.execute('UPDATE task_runs SET worker_pid=NULL,worker_started_at=NULL,claim_expires=? WHERE id=?',(now+120,runa))
        elif evidence=='unavailable':
            with sqlite3.connect(root/'capacity.sqlite') as ledger:
                ledger.execute('UPDATE capacity_probe_runs SET board=?',(str(root/'missing-board.sqlite'),))
        monkeypatch.setenv('HERMES_KANBAN_HOME',str(bhome))
        policy((bconn,owner_home,root),monkeypatch,board_db=str(bdb),task_roles={taskb:contract(bconn)})
        monkeypatch.setattr(dispatch.time,'time',lambda:now+61)
        bcalls=[]
        # Positive review fixture now has a real completed writer run. A blocked
        # probe must preserve that history and create no reviewer run.
        before_runs=[tuple(r) for r in bconn.execute('SELECT * FROM task_runs ORDER BY id')]
        result=dispatch.dispatch_once(bconn,spawn_fn=lambda task,workspace:bcalls.append(task.id) or 987655)
        allowed=evidence in ('dead','terminal')
        assert bcalls==([taskb] if allowed else [])
        if evidence in ('live','dead'):
            assert seen==[(987654,'offline-A-fingerprint')]
        if not allowed:
            assert bconn.execute('SELECT status,claim_lock FROM tasks WHERE id=?',(taskb,)).fetchone()[:]==(lane,None)
            assert bconn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',(taskb,)).fetchone()[0]==0
            assert [tuple(r) for r in bconn.execute('SELECT * FROM task_runs ORDER BY id')]==before_runs
        print('CROSS_BOARD',lane,evidence,'A',adb,'B',bdb,'B_spawned',bcalls,'identity_reads',seen)

@pytest.mark.parametrize('lane',['ready','review'])
@pytest.mark.parametrize('contract_kind',['self','unlisted','writer_review','unknown_role'])
def test_role_and_unlisted_nonmutation(board,monkeypatch,lane,contract_kind):
    conn,home,root=board
    task=card(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=?,assignee=? WHERE id=?',(lane,None if contract_kind=='unlisted' else 'codex-worker',task))
    c={'role':'independent_reviewer','writer_task_id':task if contract_kind=='self' else 'other','reviewed_sha':'a'*40}
    if contract_kind=='writer_review':
        c={'role':'writer'}
    if contract_kind=='unknown_role': c={'role':'unknown'}
    policy(board,monkeypatch,task_roles={} if contract_kind=='unlisted' else {task:c})
    monkeypatch.setattr(dispatch,'review_dispatch_enabled',lambda:True)
    before=tuple(conn.execute('SELECT status,assignee,claim_lock,consecutive_failures FROM tasks WHERE id=?',(task,)).fetchone())
    calls=[]
    result=dispatch.dispatch_once(conn,spawn_fn=lambda task,workspace:calls.append(task.id),default_assignee='codex-worker')
    if lane=='ready' and contract_kind=='writer_review':
        assert calls==[task] and result.auto_assigned_default==[]
        assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==1
        return
    assert calls==[] and result.auto_assigned_default==[]
    assert tuple(conn.execute('SELECT status,assignee,claim_lock,consecutive_failures FROM tasks WHERE id=?',(task,)).fetchone())==before
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0]==0

@pytest.mark.parametrize('lane',['ready','review'])
def test_binding_failure_no_spawn_claim_preserved(board,monkeypatch,lane):
    conn,home,root=board
    task=card(conn)
    with kb.write_txn(conn): conn.execute('UPDATE tasks SET status=? WHERE id=?',(lane,task))
    policy(board,monkeypatch,task_roles={task:completed_writer_contract(board) if lane=='review' else {'role':'writer'}})
    monkeypatch.setattr(dispatch,'review_dispatch_enabled',lambda:True)
    now=dispatch.time.time()
    b=CapacityBreaker(root/'capacity.sqlite',enabled=True)
    assert b.rate_limited(os.path.normcase(str(home.resolve())),'openai-codex',now=now-301)
    monkeypatch.setattr(CapacityBreaker,'bind_probe',lambda *args,**kw:False)
    calls=[]
    res=dispatch.dispatch_once(conn,spawn_fn=lambda task,workspace:calls.append(task.id))
    assert calls==[] and (task,'factory_probe_binding_failed') in res.respawn_guarded
    row=conn.execute('SELECT status,claim_lock,consecutive_failures FROM tasks WHERE id=?',(task,)).fetchone()
    assert row['status']=='running' and row['claim_lock'] and row['consecutive_failures']==0
    assert conn.execute('SELECT worker_pid FROM task_runs').fetchone()[0] is None


def test_actual_identity_logic_and_missing_bound_row(tmp_path,monkeypatch):
    db=tmp_path/'identity-board.sqlite'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE task_runs(id TEXT,worker_pid INT,worker_started_at TEXT,ended_at REAL,claim_expires REAL)')
        c.execute('INSERT INTO task_runs VALUES(?,?,?,?,?)',('a',987654,'offline|fingerprint',None,100))
    monkeypatch.setattr(kb,'_pid_alive',lambda pid:pid==987654)
    monkeypatch.setattr(dispatch,'_process_fingerprint',lambda pid:'offline|fingerprint' if pid==987654 else None)
    assert dispatch._factory_probe_alive(str(db),'a') is True
    monkeypatch.setattr(dispatch,'_process_fingerprint',lambda pid:'different|fingerprint')
    assert dispatch._factory_probe_alive(str(db),'a') is False
    assert dispatch._factory_probe_alive(str(db),'missing') is None
    assert dispatch._factory_probe_alive(str(tmp_path/'absent.sqlite'),'a') is None
    assert not (tmp_path/'absent.sqlite').exists()


# Native integration of the opted-in Windows Factory backend.
pytestmark = pytest.mark.platforms("windows")
