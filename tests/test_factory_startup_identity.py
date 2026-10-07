"""Native CLI/SQLite registration and completion; no credential/model calls."""
import json, os, sys, types
import pytest
from tests.test_provider_capacity_dispatch import board
from tests.test_factory_control_scope import bound_review
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from hermes_cli.cli_single_query import _run_single_query_mode
from tools import kanban_tools as kt

class SessionClaimReached(RuntimeError):
    pass

@pytest.mark.parametrize('inherited', [None, 'dispatcher-parent-session'])
@pytest.mark.parametrize('rotate', [False, True])
def test_startup28_own_identity_survives_completion(board, monkeypatch, inherited, rotate):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert dispatch.dispatch_once(conn, spawn_fn=lambda *a: None).spawned
    dispatch._set_worker_pid(conn, task, os.getpid())
    run = kb._current_run_id(conn, task)
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
    if inherited is None:
        monkeypatch.delenv('HERMES_SESSION_ID', raising=False)
    else:
        monkeypatch.setenv('HERMES_SESSION_ID', inherited)
    if hasattr(kt, '_worker_run_session_ids'):
        monkeypatch.setattr(kt, '_worker_run_session_ids', {})
    facade = types.ModuleType('cli')
    for name in ('_SeededQueryMessage', '_collect_kanban_task_images', '_collect_query_images', '_configure_quiet_agent', '_finalize_single_query', '_route_single_query_images', '_run_kanban_goal_loop_chat', '_run_quiet_single_query', '_single_query_exit_code'):
        setattr(facade, name, lambda *a, **k: None)
    facade._should_seed_interactive = lambda *a, **k: False
    monkeypatch.setitem(sys.modules, 'cli', facade)
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda *a, **k: types.SimpleNamespace())
    def stop_before_credentials(*a, **k):
        raise SessionClaimReached('UI boundary; no credentials or models')
    cli = types.SimpleNamespace(session_id='fresh-native-cli-session', _claim_active_session=stop_before_credentials)
    with pytest.raises(SessionClaimReached):
        _run_single_query_mode(cli, 'fixture', None, True, True)
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='factory_worker_started'", (task, run)).fetchall()
    assert len(rows) == 1
    identity = json.loads(rows[0]['payload'])
    assert identity['session_id'] == cli.session_id
    assert identity['pid'] == os.getpid()
    monkeypatch.setenv('HERMES_SESSION_ID', 'compressed-session' if rotate else cli.session_id)
    saved = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
    stamped = kt._stamp_worker_session_metadata(task, {'factory_review': saved['factory_review'], 'worker_session_id': 'model-forged'})
    assert stamped['worker_session_id'] == cli.session_id
    assert kb.complete_task(conn, task, result='bounded fixture', metadata=stamped, expected_run_id=run, fire_lifecycle_hook=False)
    assert kb.get_task(conn, task).status == 'done'


"""Registration failures follow durable admission, not an inherited policy path."""
import json, os, sqlite3
import pytest
from tests.test_provider_capacity_dispatch import board, card, policy
from tests.test_factory_control_scope import bound_review
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from tools import kanban_tools as kt

@pytest.mark.parametrize('kind', ['legacy_missing', 'legacy_disabled', 'factory_present', 'factory_env_removed', 'factory_metadata_replaced'])
def test_registration30_error_uses_durable_admission(board, monkeypatch, kind):
    conn, home, root = board
    is_factory = kind.startswith('factory')
    if is_factory:
        task, writer, repo, sha = bound_review(board, monkeypatch)
        assert dispatch.dispatch_once(conn, spawn_fn=lambda *a: None).spawned
    else:
        task = card(conn)
        assert kb.claim_task(conn, task)
        if kind == 'legacy_disabled': policy(board, monkeypatch, enabled=False)
        else: monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(root/'missing-policy.json'))
    run = kb._current_run_id(conn, task)
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
    if kind == 'factory_env_removed': monkeypatch.delenv('HERMES_FACTORY_ROUTING_POLICY', raising=False)
    if kind == 'factory_metadata_replaced':
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', ('{}', run)); conn.commit()
        monkeypatch.delenv('HERMES_FACTORY_ROUTING_POLICY', raising=False)
    def unavailable(*a, **k): raise sqlite3.OperationalError('bounded registration error')
    monkeypatch.setattr(dispatch, 'adopt_worker_pid', unavailable)
    assert kt.register_current_worker_from_env(worker_session_id='native-worker-origin') is (not is_factory)
    assert kb.get_task(conn, task).status == 'running'


@pytest.mark.parametrize('admitted', [False, True])
def test_admission33_board_unreadable_is_not_inherited_policy(board, monkeypatch, admitted):
    conn, home, root = board
    task = card(conn)
    assert kb.claim_task(conn, task)
    run = kb._current_run_id(conn, task)
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
    monkeypatch.setenv('HERMES_FACTORY_ROUTING_POLICY', str(root/'missing.json'))
    if admitted:
        monkeypatch.setenv('HERMES_KANBAN_FACTORY_RUN', f'{task}:{run}')
    else:
        monkeypatch.delenv('HERMES_KANBAN_FACTORY_RUN', raising=False)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('board unavailable before admission read')
    monkeypatch.setattr(kt, '_board', unavailable)
    assert kt.register_current_worker_from_env(worker_session_id='native-origin') is (not admitted)


def test_admission33_metadata_loss_stops_reviewer_after_successful_adoption(board, monkeypatch):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert dispatch.dispatch_once(conn, spawn_fn=lambda *a: None).spawned
    run = kb._current_run_id(conn, task)
    dispatch._set_worker_pid(conn, task, os.getpid())
    conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', ('{}', run));conn.commit()
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
    assert kb.bind_factory_worker_identity(conn, task, run, 'native-origin') is False
    assert kt.register_current_worker_from_env(worker_session_id='native-origin') is False
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE run_id=? AND kind='factory_worker_started'", (run,)).fetchone()[0] == 0


def test_admission33_dispatch_carries_factory_admission_once(board, monkeypatch):
    conn, home, root = board
    task = card(conn)
    policy(board, monkeypatch, task_roles={task: 'writer'})
    calls = []
    def spawned(task, workspace, *, board=None, factory_admitted=False):
        calls.append(factory_admitted)
    assert dispatch.dispatch_once(conn, spawn_fn=spawned).spawned
    assert calls == [True]


@pytest.mark.parametrize('corruption', ['shape', 'readonly', 'run_type', 'writer_id', 'sha', 'workspace'])
@pytest.mark.parametrize('copies', ['outer', 'all'])
def test_contract36_corrupt_matching_binding_never_reaches_model(board, monkeypatch, corruption, copies):
    conn, home, root = board
    task, writer, repo, sha = bound_review(board, monkeypatch)
    assert dispatch.dispatch_once(conn, spawn_fn=lambda *a: None).spawned
    run = kb._current_run_id(conn, task)
    dispatch._set_worker_pid(conn, task, os.getpid())
    assert dispatch.adopt_worker_pid(conn, task, run, os.getpid())
    saved = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run,)).fetchone()[0])
    bound = dict(saved['factory_review'])
    if corruption == 'shape': bound = {'unexpected': True}
    elif corruption == 'readonly': bound['read_only'] = False
    elif corruption == 'run_type': bound['writer_run_id'] = True
    elif corruption == 'writer_id': bound['writer_task_id'] = None
    elif corruption == 'sha': bound['reviewed_sha'] = 'not-a-commit'
    else: bound['source_workspace'] = 'relative/workspace'
    saved['factory_review'] = bound
    if copies == 'all':
        saved['factory_capacity']['factory_review'] = bound
        conn.execute("UPDATE task_events SET payload=? WHERE run_id=? AND kind='factory_capacity_permit'", (json.dumps(saved['factory_capacity']), run))
    conn.execute('UPDATE task_runs SET metadata=? WHERE id=?', (json.dumps(saved), run))
    conn.execute("UPDATE task_events SET payload=? WHERE run_id=? AND kind='factory_review_bound'", (json.dumps(bound), run));conn.commit()
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
    monkeypatch.setattr(kt, '_worker_run_session_ids', {})
    facade = types.ModuleType('cli')
    for name in ('_SeededQueryMessage', '_collect_kanban_task_images', '_collect_query_images', '_configure_quiet_agent', '_finalize_single_query', '_route_single_query_images', '_run_kanban_goal_loop_chat', '_run_quiet_single_query', '_single_query_exit_code'):
        setattr(facade, name, lambda *a, **k: None)
    facade._should_seed_interactive = lambda *a, **k: False
    monkeypatch.setitem(sys.modules, 'cli', facade)
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda *a, **k: types.SimpleNamespace())
    def forbidden_boundary(*a, **k):
        raise AssertionError('Malformed binding reached the pre-model boundary')
    native = types.SimpleNamespace(session_id='fresh-native-origin', _claim_active_session=forbidden_boundary)
    with pytest.raises(SystemExit):
        _run_single_query_mode(native, 'fixture', None, True, True)
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE run_id=? AND kind='factory_worker_started'", (run,)).fetchone()[0] == 0


# Native integration of the opted-in Windows Factory backend.
pytestmark = pytest.mark.platforms("windows")
