"""Host-native schema invariants; no model, network, or production state."""
import pytest
from hermes_cli.factory_contracts import valid_review_contract, valid_capacity_permit


def review(root):
    return {'writer_task_id': 'writer', 'source_workspace': str(root),
            'reviewed_sha': 'a' * 40, 'read_only': True,
            'writer_run_id': 7, 'writer_session_id': 'origin-session'}


@pytest.mark.parametrize('mutation', ['none', 'unknown_key', 'missing', 'readonly', 'run_bool', 'self', 'session', 'sha', 'relative', 'null_path'])
def test_review_schema_is_exact_and_host_native(tmp_path, mutation):
    value = review(tmp_path)
    if mutation == 'unknown_key': value['extra'] = True
    elif mutation == 'missing': value.pop('writer_run_id')
    elif mutation == 'readonly': value['read_only'] = 1
    elif mutation == 'run_bool': value['writer_run_id'] = True
    elif mutation == 'self': value['writer_task_id'] = 'reviewer'
    elif mutation == 'session': value['writer_session_id'] = 'invalid session'
    elif mutation == 'sha': value['reviewed_sha'] = 'not-a-sha'
    elif mutation == 'relative': value['source_workspace'] = 'relative'
    elif mutation == 'null_path': value['source_workspace'] = str(tmp_path) + '\0'
    assert valid_review_contract(value, reviewer_id='reviewer') is (mutation == 'none')


@pytest.mark.parametrize('role', ['writer', 'test_fix', 'read_only', 'independent_reviewer'])
@pytest.mark.parametrize('mutation', ['none', 'role_list', 'board', 'unknown_key', 'generation', 'probe', 'review_contract'])
def test_permit_preserves_exact_board_role_and_generation(tmp_path, role, mutation):
    board = tmp_path / 'board.sqlite'
    value = {'owner': str(tmp_path), 'provider': 'fixture', 'ledger': str(tmp_path / 'capacity.sqlite'),
             'board': str(board), 'role': role, 'probe_token': None, 'generation': None}
    if role == 'independent_reviewer': value['factory_review'] = review(tmp_path)
    if mutation == 'role_list': value['role'] = []
    elif mutation == 'board': value['board'] = str(tmp_path / 'foreign.sqlite')
    elif mutation == 'unknown_key': value['extra'] = True
    elif mutation == 'generation': value['generation'] = True
    elif mutation == 'probe': value.update(probe_token='8:nonce', generation=7)
    elif mutation == 'review_contract': value['factory_review'] = {'unexpected': True}
    assert valid_capacity_permit(value, board_path=board, reviewer_id='reviewer') is (mutation == 'none')
    if mutation == 'none':
        value.update(probe_token='7:nonce', generation=7)
        assert valid_capacity_permit(value, board_path=board, reviewer_id='reviewer')
