"""Pure schema checks shared by Factory startup and terminal acceptance."""
from pathlib import Path
import re

_REVIEW_KEYS = {'writer_task_id', 'source_workspace', 'reviewed_sha', 'read_only', 'writer_run_id', 'writer_session_id'}
_PERMIT_KEYS = {'owner', 'provider', 'ledger', 'board', 'role', 'probe_token', 'generation'}
_ROLES = {'writer', 'test_fix', 'read_only', 'independent_reviewer'}


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(c.isspace() for c in value)


def _absolute_path(value):
    return isinstance(value, str) and bool(value) and '\0' not in value and Path(value).is_absolute()


def valid_review_contract(value, *, reviewer_id):
    return bool(isinstance(value, dict) and set(value) == _REVIEW_KEYS
        and value['read_only'] is True
        and _identity(value['writer_task_id']) and value['writer_task_id'] != reviewer_id
        and type(value['writer_run_id']) is int and value['writer_run_id'] > 0
        and _identity(value['writer_session_id'])
        and isinstance(value['reviewed_sha'], str) and re.fullmatch(r'[0-9a-f]{40}', value['reviewed_sha'])
        and _absolute_path(value['source_workspace']))


def valid_capacity_permit(value, *, board_path, reviewer_id):
    if not isinstance(value, dict) or not isinstance(value.get('role'), str) or value['role'] not in _ROLES:
        return False
    review = value['role'] == 'independent_reviewer'
    if set(value) != _PERMIT_KEYS | ({'factory_review'} if review else set()):
        return False
    if any(not _absolute_path(value[k]) for k in ('owner', 'ledger', 'board')):
        return False
    if Path(value['board']) != Path(board_path):
        return False
    if not isinstance(value['provider'], str) or not value['provider'].strip():
        return False
    token, generation = value['probe_token'], value['generation']
    if token is None:
        if generation is not None:
            return False
    elif (not _identity(token) or type(generation) is not int or generation < 0
            or not token.startswith(str(generation) + ':') or not token.split(':', 1)[-1]):
        return False
    return not review or valid_review_contract(value['factory_review'], reviewer_id=reviewer_id)
