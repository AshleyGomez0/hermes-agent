"""Offline dispatcher integration; spawn fixture replaces model transport entirely."""
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.delenv('HERMES_FACTORY_ROUTING_POLICY', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda name: True)
    monkeypatch.setattr(dispatch, '_memory_pressure_level', lambda: 'unknown')
    monkeypatch.setattr(dispatch, 'count_running_tasks_other_boards', lambda board: 0)
    monkeypatch.setattr(dispatch._kbw, 'resolve_workspace', lambda task, board=None: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn, home, tmp_path


def policy(board, monkeypatch, **changes):
    conn, home, tmp_path = board
    data = {'schema_version': 1, 'enabled': True, 'owner_home': str(home),
            'board_db': str(kb.kanban_db_path()),
            'routes': {'codex': {'profile': 'codex-worker', 'provider': 'openai-codex',
                                'model': 'gpt-6.1-sol', 'eligible_roles': ['writer', 'independent_reviewer']},
                       'minimax': {'preserved': True}},
            'require_independent_reviewer': True,
            'qwen': {'bounded_only': True, 'tools': False}, 'deterministic': 'scripts'}
    data.update(changes)
    path = tmp_path / 'routing.json'
    path.write_text(json.dumps(data))
    monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(path))
    return path


def card(conn, provider='openai-codex', model='gpt-6.1-sol'):
    task = kb.create_task(conn, title='writer is not role metadata', assignee='codex-worker',
                          initial_status='blocked', model_override=model, provider_override=provider)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
    return task


def completed_writer_contract(board):
    """Real committed, clean fixture writer for positive independent-review tests."""
    import subprocess
    import tempfile
    conn, home, root = board
    repo = Path(tempfile.mkdtemp(prefix='review-writer-', dir=root))
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args],
                                       text=True, timeout=15).strip()
    git('-c', 'init.defaultBranch=main', 'init')
    git('-c', 'user.name=Offline', '-c', 'user.email=offline@example.invalid',
        'commit', '--allow-empty', '-m', 'independent review fixture')
    writer = card(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind='dir',workspace_path=? WHERE id=?", (str(repo), writer))
    assert kb.claim_task(conn, writer)
    assert kb.complete_task(conn, writer, result='fixture writer completed',
        metadata={'worker_session_id': 'fixture-worker:'+writer},
        expected_run_id=kb._current_run_id(conn, writer), fire_lifecycle_hook=False)
    return {'role': 'independent_reviewer', 'writer_task_id': writer,
            'reviewed_sha': git('rev-parse', 'HEAD')}


def tick(conn):
    # Declared UNIT fixture: no model request, subprocess or live runtime.
    calls = []
    result = dispatch.dispatch_once(conn, spawn_fn=lambda task, workspace: calls.append(task.id))
    return result, calls


def test_explicit_malformed_policy_defers_before_claim(board, monkeypatch):
    conn, home, tmp_path = board
    path = tmp_path / 'broken.json'
    path.write_text('{')
    monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(path))
    task = card(conn)
    result, calls = tick(conn)
    assert calls == []
    assert (task, 'factory_policy_invalid') in result.respawn_guarded
    row = conn.execute('SELECT status, consecutive_failures, claim_lock FROM tasks WHERE id=?', (task,)).fetchone()
    assert tuple(row) == ('ready', 0, None)
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
    assert not (home / 'kanban' / 'provider_capacity.sqlite').exists()


def test_missing_policy_preserves_dispatch(board):
    conn, home, tmp_path = board
    task = card(conn)
    result, calls = tick(conn)
    assert calls == [task]
    assert len(result.spawned) == 1
    assert not (home / 'kanban' / 'provider_capacity.sqlite').exists()


def test_disabled_policy_preserves_dispatch_without_capacity_io(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch, enabled=False)
    task = card(conn)
    result, calls = tick(conn)
    assert calls == [task]
    assert not (home / 'kanban' / 'provider_capacity.sqlite').exists()


def test_matched_override_without_role_adapter_stays_queued(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch)
    task = card(conn)
    result, calls = tick(conn)
    assert calls == []
    assert (task, 'factory_missing_role_adapter') in result.respawn_guarded
    assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'ready'
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


def test_unrelated_provider_unlisted_is_deferred(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch)
    task = card(conn, 'other-provider', 'other-model')
    result, calls = tick(conn)
    assert calls == []
    assert (task, 'factory_missing_role_adapter') in result.respawn_guarded
    assert conn.execute('SELECT provider_override FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'other-provider'


@pytest.mark.parametrize('lane,role', [('ready', 'writer'), ('ready', 'read_only'), ('ready', 'test_fix'), ('review', 'independent_reviewer')])
def test_explicit_roles_dispatch_and_persist_permit(board, monkeypatch, lane, role):
    conn, home, tmp_path = board
    task = card(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, task))
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    contract = completed_writer_contract(board) if role == 'independent_reviewer' else {'role': role}
    policy(board, monkeypatch, task_roles={task: contract}, routes={
        'codex': {'profile': 'codex-worker', 'provider': 'openai-codex', 'model': 'gpt-6.1-sol',
                  'eligible_roles': ['writer', 'read_only', 'test_fix', 'independent_reviewer']},
        'minimax': {'preserved': True}})
    assert tick(conn)[1] == [task]
    metadata = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE task_id=?', (task,)).fetchone()[0])
    import os
    assert metadata['factory_capacity']['owner'] == os.path.normcase(str(home.resolve()))
    assert (tmp_path / 'capacity.sqlite').exists()
    assert not (home / 'kanban' / 'provider_capacity.sqlite').exists()


@pytest.mark.parametrize('contract', [{'role': 'bogus'}, {'role': 'independent_reviewer', 'writer_task_id': 'SELF', 'reviewed_sha': 'a'*40}, {'role': 'independent_reviewer', 'writer_task_id': 'other', 'reviewed_sha': 'bad'}])
def test_bad_role_contract_blocks(board, monkeypatch, contract):
    conn, home, tmp_path = board
    task = card(conn)
    if contract.get('writer_task_id') == 'SELF':
        contract = dict(contract, writer_task_id=task)
    policy(board, monkeypatch, task_roles={task: contract})
    assert tick(conn)[1] == []
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
    assert not (tmp_path / 'capacity.sqlite').exists()



def test_enabled_policy_wrong_board_fails_closed(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch, board_db=str(tmp_path / 'foreign.sqlite'))
    task = card(conn)
    result, calls = tick(conn)
    assert calls == []
    assert (task, 'factory_policy_invalid') in result.respawn_guarded


def test_missing_referenced_policy_preserves_dispatch(board, monkeypatch):
    conn, home, tmp_path = board
    monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(tmp_path / 'absent.json'))
    task = card(conn)
    assert tick(conn)[1] == [task]
    assert not (home / 'kanban' / 'provider_capacity.sqlite').exists()


def test_review_lane_does_not_invent_independence(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch)
    task = card(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task,))
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    result, calls = tick(conn)
    assert calls == []
    assert (task, 'factory_missing_role_adapter') in result.respawn_guarded
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0


def test_capacity_429_opens_before_requeue_and_does_not_block_other_provider(board, monkeypatch):
    import os
    import sqlite3
    from hermes_cli.provider_capacity import CapacityBreaker
    conn, home, tmp_path = board
    a, b = card(conn), card(conn, 'provider-b', 'model-b')
    path = policy(board, monkeypatch, task_roles={a: 'writer', b: 'writer'})
    data = json.loads(path.read_text())
    data['routes']['b'] = {'profile': 'codex-worker', 'provider': 'provider-b', 'model': 'model-b', 'eligible_roles': ['writer']}
    path.write_text(json.dumps(data))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (b,))
    assert tick(conn)[1] == [a]
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=987654, started_at=0 WHERE id=?', (a,))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (b,))
    monkeypatch.setattr(dispatch, '_worker_alive', lambda pid, started_at: False)
    dispatch._record_worker_exit(987654, dispatch._wait_status_from_returncode(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    try:
        result, calls = tick(conn)
        assert a in result.rate_limited and calls == [b]
        assert tuple(conn.execute('SELECT status,consecutive_failures,claim_lock FROM tasks WHERE id=?', (a,)).fetchone()) == ('ready', 0, None)
        with sqlite3.connect(tmp_path / 'capacity.sqlite') as ledger:
            state, eligible = ledger.execute("SELECT state,eligible_at FROM capacity WHERE provider='openai-codex'").fetchone()
        assert state == 'OPEN' and eligible > dispatch.time.time()
    finally:
        dispatch._recent_worker_exits.pop(987654, None)


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_halfopen_single_permit_and_recovered_success_cannot_clear_new_open(board, monkeypatch, lane):
    import os
    from hermes_cli.provider_capacity import CapacityBreaker
    conn, home, tmp_path = board
    a, b = card(conn), card(conn)
    contracts = {t: completed_writer_contract(board) if lane == 'review' else {'role': 'writer'} for t in (a,b)}
    policy(board, monkeypatch, task_roles=contracts)
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    monkeypatch.setattr(dispatch, 'check_respawn_guard', lambda *a, **k: None)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id IN (?,?)', (lane,a,b))
    breaker = CapacityBreaker(tmp_path / 'capacity.sqlite', enabled=True)
    owner = os.path.normcase(str(home.resolve()))
    now = dispatch.time.time()
    assert breaker.rate_limited(owner, 'openai-codex', now=now-301)
    calls = tick(conn)[1]
    assert len(calls) == 1
    chosen = calls[0]
    run = conn.execute('SELECT id,metadata FROM task_runs WHERE task_id=?', (chosen,)).fetchone()
    permit = json.loads(run['metadata'])['factory_capacity']
    assert permit['probe_token'] and permit['generation']
    assert breaker.rate_limited(owner, 'openai-codex', now=now)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET outcome='completed',status='done',ended_at=?,metadata=NULL WHERE id=?", (now,run['id']))
        conn.execute("UPDATE tasks SET status='done',claim_lock=NULL,current_run_id=NULL WHERE id=?", (chosen,))
    tick(conn)  # restart-style durable outcome recovery from event, not memory
    assert breaker.acquire(owner, 'openai-codex', now=now).state == 'OPEN'


def test_recovered_matching_probe_success_closes(board, monkeypatch):
    import os
    from hermes_cli.provider_capacity import CapacityBreaker
    conn, home, tmp_path = board
    task = card(conn)
    policy(board, monkeypatch, task_roles={task: 'writer'})
    owner = os.path.normcase(str(home.resolve()))
    breaker = CapacityBreaker(tmp_path / 'capacity.sqlite', enabled=True)
    now = dispatch.time.time()
    breaker.rate_limited(owner, 'openai-codex', now=now-301)
    assert tick(conn)[1] == [task]
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET outcome='completed',status='done',ended_at=?,metadata=NULL WHERE task_id=?", (now,task))
        conn.execute("UPDATE tasks SET status='done',claim_lock=NULL,current_run_id=NULL WHERE id=?", (task,))
    tick(conn)
    assert breaker.acquire(owner, 'openai-codex', now=now).state == 'CLOSED'


@pytest.mark.parametrize('reset', [None, 2000000000.0, float('inf')])
@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_synchronous_spawn_http429_is_failure_neutral(board, monkeypatch, lane, reset):
    import sqlite3
    conn, home, tmp_path = board
    task = card(conn)
    contract = completed_writer_contract(board) if lane == 'review' else {'role': 'writer'}
    policy(board, monkeypatch, task_roles={task: contract})
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (lane, task))
    class HTTP429(Exception):
        status_code = 429
    def spawn(task, workspace):
        error = HTTP429('HTTP 429 quota wall')
        error.reset_at = reset
        raise error
    result = dispatch.dispatch_once(conn, spawn_fn=spawn)
    assert task in result.rate_limited and not result.spawned
    assert tuple(conn.execute('SELECT status,consecutive_failures,claim_lock FROM tasks WHERE id=?', (task,)).fetchone()) == (lane, 0, None)
    assert conn.execute('SELECT outcome FROM task_runs WHERE task_id=?', (task,)).fetchone()[0] == 'rate_limited'
    with sqlite3.connect(tmp_path / 'capacity.sqlite') as ledger:
        state, eligible = ledger.execute('SELECT state,eligible_at FROM capacity').fetchone()
        assert state == 'OPEN'
        if reset == 2000000000.0:
            assert eligible == reset


@pytest.mark.parametrize('reset', [2000000000.0, float('nan'), float('inf'), -1, True, 'bad'])
def test_observed_worker429_reset_is_finite(board, monkeypatch, reset):
    import sqlite3
    conn, home, tmp_path = board
    task = card(conn)
    policy(board, monkeypatch, task_roles={task: 'writer'})
    assert tick(conn)[1] == [task]
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=987654,started_at=0 WHERE id=?', (task,))
    monkeypatch.setattr(dispatch, '_worker_alive', lambda *a: False)
    monkeypatch.setattr(dispatch, '_worker_final_output', lambda *a, **k: json.dumps({'status_code': 429, 'reset_at': reset}))
    dispatch._record_worker_exit(987654, dispatch._wait_status_from_returncode(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    try:
        result, calls = tick(conn)
        assert task in result.rate_limited and calls == []
        assert tuple(conn.execute('SELECT status,consecutive_failures FROM tasks WHERE id=?', (task,)).fetchone()) == ('ready', 0)
        with sqlite3.connect(tmp_path / 'capacity.sqlite') as ledger:
            state, eligible = ledger.execute('SELECT state,eligible_at FROM capacity').fetchone()
        assert state == 'OPEN'
        if reset == 2000000000.0:
            assert eligible == reset
        else:
            assert dispatch.time.time() < eligible < dispatch.time.time() + 400
    finally:
        dispatch._recent_worker_exits.pop(987654, None)


@pytest.mark.parametrize('changed', ['unlisted', 'invalid', 'disabled'])
def test_active429_uses_saved_permit_after_policy_change(board, monkeypatch, changed):
    import sqlite3
    conn, home, tmp_path = board
    task = card(conn)
    path = policy(board, monkeypatch, task_roles={task: 'writer'})
    assert tick(conn)[1] == [task]
    run_id = conn.execute('SELECT current_run_id FROM tasks WHERE id=?', (task,)).fetchone()[0]
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=987654,started_at=0 WHERE id=?', (task,))
        conn.execute('UPDATE task_runs SET metadata=NULL WHERE id=?', (run_id,))
    if changed == 'invalid':
        path.write_text('{')
    else:
        data = json.loads(path.read_text())
        data['task_roles'] = {}
        data['enabled'] = changed != 'disabled'
        path.write_text(json.dumps(data))
    monkeypatch.setattr(dispatch, '_worker_alive', lambda *a: False)
    dispatch._record_worker_exit(987654, dispatch._wait_status_from_returncode(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    try:
        result, calls = tick(conn)
        assert task in result.rate_limited and calls == []
        with sqlite3.connect(tmp_path / 'capacity.sqlite') as ledger:
            assert ledger.execute('SELECT state FROM capacity').fetchone()[0] == 'OPEN'
        assert tuple(conn.execute('SELECT status,consecutive_failures,claim_lock FROM tasks WHERE id=?', (task,)).fetchone()) == ('ready', 0, None)
        assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 1
    finally:
        dispatch._recent_worker_exits.pop(987654, None)


def test_disabled_existing_429_requeue_is_failure_neutral(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch, enabled=False)
    task = card(conn)
    assert tick(conn)[1] == [task]
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=987654, started_at=0 WHERE id=?', (task,))
    monkeypatch.setattr(dispatch, '_worker_alive', lambda pid, started_at: False)
    dispatch._record_worker_exit(987654, dispatch._wait_status_from_returncode(kb.KANBAN_RATE_LIMIT_EXIT_CODE))
    try:
        result, calls = tick(conn)
        assert calls == []
        assert task in result.rate_limited
        row = conn.execute('SELECT status, consecutive_failures, claim_lock FROM tasks WHERE id=?', (task,)).fetchone()
        assert tuple(row) == ('ready', 0, None)
        assert not (home / 'kanban' / 'provider_capacity.sqlite').exists()
    finally:
        dispatch._recent_worker_exits.pop(987654, None)


def test_unlisted_task_must_not_be_auto_assigned(board, monkeypatch):
    conn, home, tmp_path = board
    policy(board, monkeypatch)
    task = card(conn)
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET assignee=NULL WHERE id=?', (task,))
    result = dispatch.dispatch_once(conn, spawn_fn=lambda *args: None, default_assignee='codex-worker')
    actual = conn.execute('SELECT assignee,status FROM tasks WHERE id=?', (task,)).fetchone()
    print('UNLISTED_RESULT', tuple(actual), 'auto_assigned_default', result.auto_assigned_default)
    assert actual['assignee'] is None, 'Enabled Factory policy mutated an unlisted task assignment'


def test_no_second_probe_while_first_worker_still_running(board, monkeypatch):
    conn, home, tmp_path = board
    a, b = card(conn), card(conn)
    policy(board, monkeypatch, task_roles={a:'writer', b:'writer'})
    monkeypatch.setattr(dispatch, 'check_respawn_guard', lambda *a, **k: None)
    monkeypatch.setattr(dispatch, '_worker_alive', lambda *a, **k: True)
    from hermes_cli.provider_capacity import CapacityBreaker
    import os
    breaker = CapacityBreaker(tmp_path / 'capacity.sqlite', enabled=True)
    owner = os.path.normcase(str(home.resolve()))
    now = dispatch.time.time()
    assert breaker.rate_limited(owner, 'openai-codex', now=now-301)
    calls = []
    first = dispatch.dispatch_once(conn, spawn_fn=lambda task, workspace: calls.append(task.id) or 987654)
    assert len(calls) == 1
    first_task = calls[0]
    monkeypatch.setattr(dispatch.time, 'time', lambda: now+61)
    second = dispatch.dispatch_once(conn, spawn_fn=lambda task, workspace: calls.append(task.id) or 987655)
    running = conn.execute("SELECT id,worker_pid FROM tasks WHERE status='running'").fetchall()
    print('PROBE_RESULT', 'calls', calls, 'running', [tuple(r) for r in running])
    assert len(calls) == 1, 'Lease expiry admitted another probe despite first worker remaining alive'


def test_review_lane_cannot_dispatch_writer_contract(board, monkeypatch):
    conn, home, tmp_path = board
    task = card(conn)
    policy(board, monkeypatch, task_roles={task:'writer'})
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task,))
    result, calls = tick(conn)
    print('REVIEW_WRITER_RESULT', calls)
    assert calls == [], 'require_independent_reviewer accepted a writer contract for review lane'




# Native integration of the opted-in Windows Factory backend.
pytestmark = pytest.mark.platforms("windows")
