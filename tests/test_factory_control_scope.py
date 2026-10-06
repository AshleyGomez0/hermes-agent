"""Offline full-module tests; no real worker/provider transport."""
import json
import pytest
from test_provider_capacity_dispatch import board, policy, card, tick, completed_writer_contract
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch

@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_http402_preserves_phase_without_failure(board, monkeypatch, lane):
    conn, home, root = board
    task = card(conn)
    contract = completed_writer_contract(board) if lane == 'review' else 'writer'
    policy(board, monkeypatch, task_roles={task: contract})
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, task)); conn.commit()
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    def reject(*args):
        exc = RuntimeError('billing exhausted'); exc.status_code = 402; raise exc
    result = dispatch.dispatch_once(conn, spawn_fn=reject)
    assert task in result.rate_limited
    assert tuple(conn.execute('SELECT status,consecutive_failures,claim_lock FROM tasks WHERE id=?', (task,)).fetchone()) == (lane, 0, None)
    import sqlite3
    with sqlite3.connect(root / 'capacity.sqlite') as ledger:
        assert ledger.execute('SELECT state FROM capacity').fetchone()[0] == 'OPEN'
    event = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='rate_limited' ORDER BY id DESC", (task,)).fetchone()
    assert json.loads(event[0])['status_code'] == 402


def scope(board, monkeypatch, **changes):
    conn, home, root = board
    data = {'schema_version': 1, 'owner_home': str(home), 'board_db': str(kb.kanban_db_path()), 'task_ids': []}
    data.update(changes)
    path = root / 'control.json'; path.write_text(json.dumps(data))
    monkeypatch.setenv('HERMES_FACTORY_CONTROL_SCOPE_FILE', str(path))
    return path

@pytest.mark.parametrize('optional', ['missing', 'disabled'])
def test_control_allowlist_independent_of_optional_policy(board, monkeypatch, optional):
    conn, home, root = board
    allowed, denied = card(conn), card(conn)
    scope(board, monkeypatch, task_ids=[allowed])
    if optional == 'disabled': policy(board, monkeypatch, enabled=False)
    else: monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(root/'absent.json'))
    result, calls = tick(conn)
    assert calls == [allowed]
    assert (denied, 'factory_control_task_denied') in result.respawn_guarded
    assert tuple(conn.execute('SELECT status,claim_lock FROM tasks WHERE id=?',(denied,)).fetchone()) == ('ready', None)

@pytest.mark.parametrize('kind', ['missing', 'malformed', 'owner', 'board'])
def test_control_invalid_scope_denies_before_assignment(board, monkeypatch, kind):
    conn, home, root = board
    task = card(conn)
    conn.execute('UPDATE tasks SET assignee=NULL WHERE id=?', (task,)); conn.commit()
    path = scope(board, monkeypatch, task_ids=[task])
    if kind == 'missing': path.unlink()
    elif kind == 'malformed': path.write_text('{')
    else:
        data = json.loads(path.read_text()); data['owner_home' if kind == 'owner' else 'board_db'] = str(root/'foreign'); path.write_text(json.dumps(data))
    result = dispatch.dispatch_once(conn, default_assignee='codex-worker', spawn_fn=lambda *a: pytest.fail('guard spawned'))
    assert (task, 'factory_control_scope_invalid') in result.respawn_guarded
    assert tuple(conn.execute('SELECT status,assignee,claim_lock FROM tasks WHERE id=?',(task,)).fetchone()) == ('ready', None, None)
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


def test_guarded_reviewer_rejects_nonexistent_writer(board, monkeypatch):
    conn, home, root = board
    task = card(conn)
    scope(board, monkeypatch, task_ids=[task])
    policy(board, monkeypatch, task_roles={task: {'role': 'independent_reviewer', 'writer_task_id': 'nonexistent-writer', 'reviewed_sha': 'a'*40}})
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task,)); conn.commit()
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    result, calls = tick(conn)
    assert calls == [], 'Reviewer must bind an existing completed writer and its actual committed workspace SHA'
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


def writer_run_id(conn, writer):
    # _end_run clears current_run_id. The latest historical attempt is durable.
    return conn.execute('SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (writer,)).fetchone()[0]


def completed_writer(board):
    import subprocess
    conn, home, root = board
    repo = root / 'writer-repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init'); git('-c', 'user.name=Offline', '-c', 'user.email=offline@example.invalid', 'commit', '--allow-empty', '-m', 'fixture')
    writer = card(conn)
    conn.execute("UPDATE tasks SET workspace_kind='dir',workspace_path=? WHERE id=?", (str(repo), writer)); conn.commit()
    assert kb.claim_task(conn, writer)
    assert kb.complete_task(conn, writer, result='fixture writer completed', metadata={'worker_session_id': 'fixture-worker:'+writer}, expected_run_id=kb._current_run_id(conn,writer), fire_lifecycle_hook=False)
    return writer, repo, git('rev-parse', 'HEAD')


def bound_review(board, monkeypatch):
    conn, home, root = board
    writer, repo, sha = completed_writer(board)
    task = card(conn)
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task,)); conn.commit()
    contract = {'role': 'independent_reviewer', 'writer_task_id': writer, 'reviewed_sha': sha}
    policy(board, monkeypatch, task_roles={task: contract})
    scope(board, monkeypatch, task_ids=[task])
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    return task, writer, repo, sha


def test_review_contract_durable_before_spawn(board, monkeypatch):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    observed = []
    def spawn(t, workspace):
        run = conn.execute('SELECT id,metadata FROM task_runs WHERE task_id=?', (task,)).fetchone()
        binding = json.loads(run['metadata'])['factory_review']
        assert binding == {'writer_task_id': writer, 'source_workspace': str(repo.resolve()), 'reviewed_sha': sha, 'read_only': True, 'writer_run_id': writer_run_id(conn, writer), 'writer_session_id': 'fixture-worker:'+writer}
        context = kb.build_worker_context(conn, task)
        assert sha in context and writer in context and 'READ_ONLY' in context
        observed.append(run['id'])
    dispatch.dispatch_once(conn, spawn_fn=spawn)
    assert observed, 'Native context must include frozen read-only binding before spawn'


@pytest.mark.parametrize('bad', ['sha', 'self', 'phase', 'path', 'git', 'writer_session_missing'])
def test_review_binding_fail_closed(board, monkeypatch, bad):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    contract = {'role': 'independent_reviewer', 'writer_task_id': writer, 'reviewed_sha': sha}
    if bad == 'sha': contract['reviewed_sha'] = 'a'*40
    elif bad == 'self': contract['writer_task_id'] = task
    elif bad == 'phase': conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (writer,))
    elif bad == 'path': conn.execute('UPDATE tasks SET workspace_path=NULL WHERE id=?', (writer,))
    elif bad == 'git': conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(root), writer))
    elif bad == 'writer_session_missing': conn.execute("UPDATE task_runs SET metadata='{}' WHERE id=?", (writer_run_id(conn, writer),))
    conn.commit(); policy(board, monkeypatch, task_roles={task: contract})
    assert tick(conn)[1] == []
    assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (task,)).fetchone()[0] == 0


def bind_review_fixture(conn, task, session=None):
    """Use the native registration binder with this isolated test process."""
    import os
    dispatch._set_worker_pid(conn, task, os.getpid())
    assert kb.bind_factory_worker_identity(conn, task, kb._current_run_id(conn, task), session or 'fixture-reviewer:'+task)


@pytest.mark.parametrize('bad', ['missing', 'sha', 'mutation', 'head', 'metadata', 'event_missing', 'event_duplicate'])
def test_review_completion_rejects_bad_callback(board, monkeypatch, bad):
    import subprocess
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert tick(conn)[1] == [task]
    run_id = kb._current_run_id(conn, task)
    bind_review_fixture(conn, task)
    metadata = {'factory_review': {'writer_task_id': writer, 'source_workspace': str(repo.resolve()), 'reviewed_sha': sha, 'read_only': True, 'writer_run_id': writer_run_id(conn, writer), 'writer_session_id': 'fixture-worker:'+writer}, 'worker_session_id': 'fixture-reviewer:'+task}
    if bad == 'event_missing':
        conn.execute("DELETE FROM task_events WHERE task_id=? AND run_id=? AND kind='factory_review_bound'", (task, run_id)); conn.commit()
    elif bad == 'event_duplicate':
        with kb.write_txn(conn):
            kb._append_event(conn, task, 'factory_review_bound', metadata['factory_review'], run_id=run_id)
    elif bad == 'missing': metadata = {}
    elif bad == 'sha': metadata['factory_review']['reviewed_sha'] = 'a'*40
    elif bad == 'mutation': (repo/'changed.txt').write_text('modified after review dispatch')
    elif bad == 'head': subprocess.check_call(['git','-C',str(repo),'-c','user.name=Offline','-c','user.email=offline@example.invalid','commit','--allow-empty','-m','later'])
    elif bad == 'metadata':
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps({'factory_review': {'reviewed_sha': 'a'*40}}), run_id)); conn.commit()
    # A claimed reviewer is RUNNING (the source lane remains review). Refusing
    # a forged callback must preserve the complete task/run, not move it back.
    before_task = tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
    before_run = tuple(conn.execute('SELECT * FROM task_runs WHERE id=?', (run_id,)).fetchone())
    assert kb.get_task(conn, task).status == 'running'
    assert kb.complete_task(conn, task, result='review accepted', metadata=metadata, expected_run_id=run_id, fire_lifecycle_hook=False) is False
    assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == before_task
    assert tuple(conn.execute('SELECT * FROM task_runs WHERE id=?', (run_id,)).fetchone()) == before_run


def test_review_completion_accepts_exact_frozen_binding(board, monkeypatch):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert tick(conn)[1] == [task]
    run_id = kb._current_run_id(conn, task)
    bind_review_fixture(conn, task)
    metadata = {'factory_review': {'writer_task_id': writer, 'source_workspace': str(repo.resolve()), 'reviewed_sha': sha, 'read_only': True, 'writer_run_id': writer_run_id(conn, writer), 'writer_session_id': 'fixture-worker:'+writer}, 'worker_session_id': 'fixture-reviewer:'+task}
    assert kb.complete_task(conn, task, result='review accepted', metadata=metadata, expected_run_id=run_id, fire_lifecycle_hook=False)
    assert json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()[0])['factory_review'] == metadata['factory_review']


def test_existing_worker_billing_exit75(monkeypatch):
    from hermes_cli.cli_single_query import _single_query_exit_code
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'offline-control')
    assert _single_query_exit_code({'failed': True, 'failure_reason': 'billing'}) == kb.KANBAN_RATE_LIMIT_EXIT_CODE == 75


# CP18 worker-execution identity regressions. Origin session_id is NOT execution identity.
@pytest.mark.parametrize('bad', ['missing', 'blank', 'same', 'writer_changed', 'run_changed'])
def test_worker_identity_rejects_unknown_self_or_changed(board, monkeypatch, bad):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert tick(conn)[1] == [task]
    run_id = kb._current_run_id(conn, task)
    binding = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()[0])['factory_review']
    bind_review_fixture(conn, task)
    metadata = {'factory_review': binding, 'worker_session_id': 'fixture-reviewer:'+task}
    if bad == 'missing': metadata.pop('worker_session_id')
    elif bad == 'blank': metadata['worker_session_id'] = ' '
    elif bad == 'same': metadata['worker_session_id'] = 'fixture-worker:'+writer
    elif bad == 'writer_changed':
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps({'worker_session_id':'changed-worker-session'}), writer_run_id(conn,writer)));conn.commit()
    elif bad == 'run_changed':
        conn.execute('UPDATE tasks SET current_run_id=999999 WHERE id=?',(writer,));conn.commit()
    before=tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone())
    assert kb.complete_task(conn,task,result='candidate verdict',metadata=metadata,expected_run_id=run_id,fire_lifecycle_hook=False) is False
    assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()) == before


def test_same_creator_origin_allows_distinct_worker_sessions(board, monkeypatch):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    conn.execute('UPDATE tasks SET session_id=? WHERE id IN (?,?)',('same-foreman-origin',task,writer));conn.commit()
    assert tick(conn)[1] == [task]
    run_id=kb._current_run_id(conn,task)
    bound=json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',(run_id,)).fetchone()[0])['factory_review']
    assert bound['writer_run_id']==writer_run_id(conn,writer)
    assert bound['writer_session_id']=='fixture-worker:'+writer
    bind_review_fixture(conn,task,'different-executor')
    assert kb.complete_task(conn,task,result='independent verdict',metadata={'factory_review':bound,'worker_session_id':'different-executor'},expected_run_id=run_id,fire_lifecycle_hook=False)


def test_uppercase_sha_normalizes_to_canonical_binding(board, monkeypatch):
    conn, home, root=board
    task,writer,repo,sha=bound_review(board,monkeypatch)
    policy(board,monkeypatch,task_roles={task:{'role':'independent_reviewer','writer_task_id':writer,'reviewed_sha':sha.upper()}})
    assert tick(conn)[1]==[task]
    binding=json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?',(kb._current_run_id(conn,task),)).fetchone()[0])['factory_review']
    assert binding['reviewed_sha']==sha


def test_worker_session_stamp_rejects_forged_metadata(monkeypatch):
    from tools.kanban_tools import _stamp_worker_session_metadata
    original={'field':'keep','worker_session_id':'forged'}
    monkeypatch.setenv('HERMES_KANBAN_TASK','own-task');monkeypatch.setenv('HERMES_SESSION_ID','real-runtime-session')
    assert _stamp_worker_session_metadata('own-task',original)['worker_session_id']=='real-runtime-session'
    assert _stamp_worker_session_metadata('foreign-task',original)=={'field':'keep'}
    monkeypatch.delenv('HERMES_SESSION_ID')
    assert _stamp_worker_session_metadata('own-task',original)=={'field':'keep'}
    assert original['worker_session_id']=='forged'


@pytest.mark.parametrize('target', ['scope', 'routing'])
def test_bom33_windows_json_preserves_admission(board, monkeypatch, target):
    conn, home, root = board
    task = card(conn)
    control = scope(board, monkeypatch, task_ids=[task])
    routing = policy(board, monkeypatch, task_roles={task: 'writer'})
    path = control if target == 'scope' else routing
    path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes())
    result, calls = tick(conn)
    assert calls == [task]
