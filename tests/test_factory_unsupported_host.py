"""A Windows-only opted-in Factory lane must not spawn on unsupported hosts."""
import pytest
from tests.test_provider_capacity_dispatch import board, card, policy, tick

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize('role', ['writer', 'test_fix'])
def test_unsupported_factory_writer_preserves_unclaimed_queue(board, monkeypatch, role):
    conn, home, root = board
    task = card(conn)
    policy(board, monkeypatch, task_roles={task: role}, routes={
        'codex': {'profile': 'codex-worker', 'provider': 'openai-codex', 'model': 'gpt-6.1-sol',
                  'eligible_roles': ['writer', 'test_fix']}, 'minimax': {'preserved': True}})
    result, calls = tick(conn)
    assert calls == []
    assert (task, 'factory_workspace_lease_unsupported_host') in result.respawn_guarded
    assert tuple(conn.execute('SELECT status,claim_lock,consecutive_failures FROM tasks WHERE id=?', (task,)).fetchone()) == ('ready', None, 0)
    assert conn.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
    assert not (root / 'capacity.sqlite').exists()
