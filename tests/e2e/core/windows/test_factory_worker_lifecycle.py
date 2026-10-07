"""Real Windows Factory spawn and callback, deterministic LOOPBACK provider only.
All homes, SQLite files and workspaces belong to the disposable test directory.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from tests.e2e.core.windows._helpers import make_home, kill_owned, wait_until
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
pytestmark = [pytest.mark.platforms('windows'), pytest.mark.integration, pytest.mark.live_system_guard_bypass]


def test_real_factory_writer_and_independent_reviewer(tmp_path, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    contracts, requests = {}, []
    def responder(rec):
        messages = rec['body']['messages']
        if messages[-1].get('role') == 'tool':
            return Text('CP41 finite canary complete')
        text = '\n'.join(str(m.get('content', '')) for m in messages)
        hit = re.search(r'work kanban task (t_[0-9a-f]+)', text)
        if not hit:
            return Text('CP41 auxiliary request')
        task_id = hit.group(1)
        requests.append(task_id); reached.set()
        if not release.wait(60):
            raise RuntimeError('parent did not acknowledge running proof')
        args = {'summary': 'CP41 native callback received'}
        if task_id in contracts:
            args['metadata'] = {'factory_review': contracts[task_id]}
        return ToolCall('kanban_complete', args)
    with FakeLLMServer(responder) as srv:
        home = make_home(tmp_path, srv.base_url)
        started = time.time()
        for key in list(os.environ):
            if key.startswith('HERMES_'):
                monkeypatch.delenv(key, raising=False)
        for key, value in home.env().items():
            monkeypatch.setenv(key, value)
        # Preserve the Windows drive needed by native shell cache expansion.
        monkeypatch.setenv('SystemDrive', Path(os.environ['SystemRoot']).drive)
        monkeypatch.chdir(home.project)
        monkeypatch.setenv('HERMES_GATEWAY_LOCK_DIR', str(tmp_path/'gateway-locks'))
        policy_path = tmp_path/'factory-routing.json'
        monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(policy_path))
        monkeypatch.delenv('HERMES_BIN', raising=False)
        repo = home.project
        def git(*args):
            return subprocess.check_output(['git','-C',str(repo),*args], text=True,
                encoding='utf-8', errors='replace', timeout=20).strip()
        git('init','-b','main')
        (repo/'canary.txt').write_text('CP41 fixture only\n', encoding='utf-8')
        git('add','canary.txt')
        git('-c','user.name=CP41 Test','-c','user.email=cp40@example.invalid','commit','-m','isolated fixture')
        sha = git('rev-parse','HEAD')
        kb.init_db()
        record = {'external_provider':False,'production_mutations':False,'runs':[]}
        try:
            with kbc.connect() as conn:
                actual_db = conn.execute('PRAGMA database_list').fetchone()[2]
                assert Path(actual_db).resolve().is_relative_to(tmp_path.resolve())
                policy = {'schema_version':1,'enabled':True,'owner_home':str(home.hermes_home),
                    'board_db':actual_db,'require_independent_reviewer':True,
                    'qwen':{'bounded_only':True,'tools':False},'deterministic':'scripts',
                    'routes':{'test':{'profile':'default','provider':'custom','model':'fake-model',
                    'eligible_roles':['writer','independent_reviewer']},'minimax':{'preserved':True}},'task_roles':{}}
                writer = None
                for role in ('writer','independent_reviewer'):
                    reached.clear();release.clear()
                    task = kb.create_task(conn,title='CP41 '+role,assignee='default',initial_status='blocked',
                        workspace_kind='dir',workspace_path=str(repo),provider_override='custom',model_override='fake-model')
                    conn.execute('UPDATE tasks SET status=? WHERE id=?',('ready' if role=='writer' else 'review',task));conn.commit()
                    contract = {'role':role}
                    if writer:
                        contract.update(writer_task_id=writer,reviewed_sha=sha)
                    policy['task_roles']={task:contract}
                    policy_path.write_text(json.dumps(policy), encoding='utf-8')
                    if writer:
                        contracts[task]=kb._factory_writer_snapshot(conn,task,writer,sha)
                        assert contracts[task]
                    result = dispatch.dispatch_once(conn,max_spawn=1)
                    assert result.spawned, str(result)
                    row=kb.get_task(conn,task);launch_pid=row.worker_pid;run_id=row.current_run_id
                    proc=dispatch._live_worker_procs[launch_pid]
                    pid=launch_pid
                    assert pid and pid != os.getpid()
                    assert reached.wait(90), 'worker never reached isolated provider: '+str(pid)
                    row=conn.execute('SELECT worker_pid,worker_started_at FROM task_runs WHERE id=?',(run_id,)).fetchone()
                    pid=row['worker_pid']
                    assert dispatch._worker_alive(pid, row['worker_started_at'])
                    assert pid != os.getpid()
                    dispatch._set_worker_pid(conn,task,launch_pid,expected_run_id=run_id)
                    assert conn.execute('SELECT worker_pid FROM task_runs WHERE id=?',(run_id,)).fetchone()[0] == pid
                    assert not dispatch.adopt_worker_pid(conn,task,run_id,os.getpid())
                    assert conn.execute('SELECT worker_pid FROM task_runs WHERE id=?',(run_id,)).fetchone()[0] == pid
                    events=[r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=? AND run_id=?',(task,run_id))]
                    assert ('factory_workspace_lease' if role=='writer' else 'factory_worker_started') in events
                    release.set()
                    wait_until(lambda: kb.get_task(conn,task).status=='done',90,'native callback')
                    assert proc.wait(timeout=90)==0
                    dispatch._set_worker_pid(conn,task,launch_pid,expected_run_id=run_id)
                    assert kb.get_task(conn,task).worker_pid is None
                    assert not dispatch.adopt_worker_pid(conn,task,run_id,os.getpid())
                    run=conn.execute('SELECT metadata,outcome FROM task_runs WHERE id=?',(run_id,)).fetchone()
                    meta=json.loads(run['metadata'])
                    assert run['outcome']=='completed' and meta['worker_session_id']
                    record['runs'].append({'role':role,'task_id':task,'run_id':run_id,'pid':pid,'launch_pid':launch_pid,
                        'session_id':meta['worker_session_id'],'outcome':run['outcome'],'events':events})
                    if writer is None:writer=task
                assert record['runs'][0]['session_id'] != record['runs'][1]['session_id']
                assert git('rev-parse','HEAD')==sha and not git('status','--porcelain')
                assert requests==[r['task_id'] for r in record['runs']]
                print('CP41_RECEIPT='+json.dumps(record),flush=True)
        finally:
            release.set()
            kill_owned(home,since=started)
