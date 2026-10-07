"""Real Windows gateway owns dispatch, reviewer handoff and restart recovery.

Only the provider is a deterministic loopback server. The board, native worker,
PID handoff, completion tool, gateway stop/restart and both sessions are real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from gateway.status import live_gateway_pid_for_home
from tests.e2e.core.windows._helpers import (
    hermes, hermes_argv, hermes_exe, kill_owned, make_home, wait_until,
)
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

pytestmark = [pytest.mark.platforms('windows'), pytest.mark.integration,
              pytest.mark.live_system_guard_bypass]


def test_gateway_drives_review_and_preserves_live_worker_across_restart(tmp_path, monkeypatch):
    ready = {role: threading.Event() for role in ('writer', 'reviewer')}
    release = {role: threading.Event() for role in ready}
    tasks = {}
    data = {}
    gateway = None
    started = time.time()

    def read(sql, args=()):
        with sqlite3.connect(Path(data['db']).as_uri() + '?mode=ro', uri=True, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, args)]

    def respond(request):
        messages = request['body'].get('messages', [])
        if messages and messages[-1].get('role') == 'tool':
            return Text('The bounded Control Plane task has completed.')
        text = str(messages)
        matches = [role for role, tid in tasks.items() if re.search(r'work kanban task ' + re.escape(tid), text)]
        if len(matches) != 1:
            return Text('Auxiliary request; no action.')
        role = matches[0]
        ready[role].set()
        if not release[role].wait(150):
            return Text('Bounded test deadline; do not claim completion.')
        metadata = {'worker_session_id': 'must-not-be-accepted-from-model'}
        if role == 'reviewer':
            rows = read("SELECT payload FROM task_events WHERE task_id=? AND kind='factory_review_bound'", (tasks[role],))
            if len(rows) != 1:
                return Text('Missing immutable review contract; no completion.')
            metadata['factory_review'] = json.loads(rows[0]['payload'])
        return ToolCall('kanban_complete', {'summary': 'CP41 gateway ' + role, 'metadata': metadata})

    def gateway_state():
        try:
            return json.loads((data['home'].hermes_home / 'gateway_state.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {}

    def launch(label):
        home = data['home']
        log = tmp_path / (label + '.log')
        with log.open('wb') as out:
            process = subprocess.Popen(hermes_argv('gateway', 'run'), cwd=home.project,
                env=home.env(), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        wait_until(lambda: (gateway_state().get('gateway_state') == 'running'
                            and live_gateway_pid_for_home(home.hermes_home)) or process.poll() is not None,
                   120, 'native gateway startup')
        assert process.poll() is None, log.read_text(encoding='utf-8-sig', errors='replace')[-4000:]
        return process, live_gateway_pid_for_home(home.hermes_home)

    with FakeLLMServer(respond) as server:
        home = make_home(tmp_path, server.base_url,
            extra_config='kanban:\n  dispatch_in_gateway: true\n  dispatch_interval_seconds: 1\n')
        data['home'] = home
        home.extra_env.update({'SystemDrive': os.environ.get('SystemDrive', 'C:'),
                               'HERMES_BIN': str(hermes_exe(home))})
        policy_path, scope_path = tmp_path / 'routing.json', tmp_path / 'control.json'
        home.extra_env.update({'HERMES_FACTORY_ROUTING_POLICY': str(policy_path),
                               'HERMES_FACTORY_CONTROL_SCOPE_FILE': str(scope_path)})
        # The exact same clean workspace is the writer output and the review input.
        repo = home.project
        def git(*args):
            result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                text=True, encoding='utf-8', errors='replace', check=True, timeout=20)
            return result.stdout.strip()
        git('init')
        (repo / 'fixture.txt').write_text('isolated Control Plane acceptance\n', encoding='utf-8')
        git('add', 'fixture.txt')
        git('-c', 'user.name=ControlPlaneTest', '-c', 'user.email=fixture@example.invalid',
            'commit', '-m', 'isolated acceptance fixture')
        sha = git('rev-parse', 'HEAD')
        for key, value in home.env().items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv('HERMES_KANBAN_HOME', str(home.hermes_home))
        monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
        monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
        kb.init_db()
        with kbc.connect_closing() as conn:
            db = next(row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main')
            assert Path(db).resolve().is_relative_to(tmp_path.resolve())
            data['db'] = db
            for role in ('writer', 'reviewer'):
                tid = kb.create_task(conn, title='CP41 persistent ' + role, assignee='default',
                    initial_status='blocked', workspace_kind='dir', workspace_path=str(repo),
                    provider_override='custom', model_override='fake-model')
                tasks[role] = tid
            kb.link_tasks(conn, tasks['writer'], tasks['reviewer'])
            conn.execute('UPDATE tasks SET status=? WHERE id=?', ('ready', tasks['writer']))
            conn.execute('UPDATE tasks SET status=? WHERE id=?', ('review', tasks['reviewer']))
            conn.commit()
        home.extra_env.update({'HERMES_KANBAN_HOME': str(home.hermes_home), 'HERMES_KANBAN_DB': db})
        policy = {'schema_version': 1, 'enabled': True, 'owner_home': str(home.hermes_home),
            'board_db': db, 'require_independent_reviewer': True,
            'qwen': {'bounded_only': True, 'tools': False}, 'deterministic': 'scripts',
            'routes': {'test': {'profile': 'default', 'provider': 'custom', 'model': 'fake-model',
                'eligible_roles': ['writer', 'independent_reviewer']}, 'minimax': {'preserved': True}},
            'task_roles': {tasks['writer']: {'role': 'writer'}, tasks['reviewer']: {
                'role': 'independent_reviewer', 'writer_task_id': tasks['writer'], 'reviewed_sha': sha}}}
        policy_path.write_text(json.dumps(policy), encoding='utf-8')
        scope_path.write_text(json.dumps({'schema_version': 1, 'owner_home': str(home.hermes_home),
            'board_db': db, 'task_ids': list(tasks.values())}), encoding='utf-8')
        try:
            gateway, first_pid = launch('first-gateway')
            assert ready['writer'].wait(120), read('SELECT id,status,last_failure_error FROM tasks')
            assert not ready['reviewer'].is_set()
            release['writer'].set()
            # No second dispatch call or policy rewrite: the gateway owns the next gate.
            assert ready['reviewer'].wait(120), read('SELECT id,status,last_failure_error FROM tasks')
            before = read('SELECT id,worker_pid,worker_started_at FROM task_runs WHERE task_id=?', (tasks['reviewer'],))
            assert len(before) == 1 and before[0]['worker_pid']
            stopped = hermes(home, 'gateway', 'stop')
            assert stopped.returncode == 0, stopped.tail()
            gateway.wait(timeout=60)
            gateway, second_pid = launch('restarted-gateway')
            assert first_pid != second_pid
            assert read('SELECT id,worker_pid,worker_started_at FROM task_runs WHERE task_id=?',
                        (tasks['reviewer'],)) == before
            release['reviewer'].set()
            wait_until(lambda: all(row['status'] == 'done' for row in read('SELECT status FROM tasks')),
                       120, 'native reviewer callback after gateway restart')
            runs = read('SELECT task_id,metadata,outcome FROM task_runs ORDER BY id')
            assert len(runs) == 2 and all(row['outcome'] == 'completed' for row in runs)
            sessions = [json.loads(row['metadata'])['worker_session_id'] for row in runs]
            assert len(set(sessions)) == 2 and 'must-not-be-accepted-from-model' not in sessions
            assert len(read("SELECT id FROM task_events WHERE kind='factory_worker_started'")) == 1
            assert git('rev-parse', 'HEAD') == sha and not git('status', '--porcelain')
            receipt = {'source_sha': git('-C', str(Path(__file__).resolve().parents[4]), 'rev-parse', 'HEAD'),
                'gateway_pids': [first_pid, second_pid], 'task_ids': tasks, 'sessions': sessions,
                'reviewer_identity_preserved': before, 'provider': 'loopback', 'production': False}
            (tmp_path / 'continuity-receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
            stopped = hermes(home, 'gateway', 'stop')
            assert stopped.returncode == 0, stopped.tail()
            gateway.wait(timeout=60)
            assert live_gateway_pid_for_home(home.hermes_home) is None
        finally:
            for event in release.values():
                event.set()
            kill_owned(home, since=started)
