"""Ashley Control Plane context: a read-only view, not a scheduler or state store.
Uses native new-session prompt sections and per-turn context for live reconnect.
Never loads auth, provider credentials, transcripts, or historical state as current.
"""
from __future__ import annotations
from pathlib import Path, PureWindowsPath
from datetime import datetime, timezone
import hashlib
import json

STATE = Path('C:/hermes-server/state/durable_multi_agent_state.json')
REGISTRY = Path('C:/hermes-server/state/canonical_registry.json')
GUARD = Path('C:/hermes-server/state/one_writer_guard.json')
MAX_BYTES = 2_000_000
PROJECT = 'hermes-server-workspace'
FAIL_CLOSED = 'CONTROL_PLANE_CONTEXT=UNVERIFIED. Authority state is missing, invalid, or inconsistent. Do not dispatch or mutate. Read-only diagnosis only; do not resume historical product work. No HEALTHY claim.'


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate state key')
        result[key] = value
    return result


def _load(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('State exceeds bounded context size')
    data = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(data, dict):
        raise ValueError('State must be an object')
    return data, hashlib.sha256(raw).hexdigest()


def _text(value, limit=220):
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid context field')
    return value


def build_view(state_path=None, registry_path=None, guard_path=None):
    state, state_sha = _load(state_path or STATE)
    registry, registry_sha = _load(registry_path or REGISTRY)
    guard, guard_sha = _load(guard_path or GUARD)
    entries = registry.get('projects')
    if not isinstance(entries, list):
        raise ValueError('Registry projects must be a list')
    matches = [x for x in entries if isinstance(x, dict) and x.get('project_id') == PROJECT]
    if len(matches) != 1:
        raise ValueError('Canonical workspace must be unique')
    entry = matches[0]
    if entry.get('logical_owner') != 'ASHLEY' or entry.get('ashley_authority') != 'CANONICAL' or PureWindowsPath(entry.get('canonical_path', '')) != PureWindowsPath('C:/hermes-server'):
        raise ValueError('Workspace registry authority mismatch')
    gate = _text(state.get('current_gate'))
    for layer in (state, state.get('session_bootstrap'), state.get('control_plane')):
        if not isinstance(layer, dict):
            raise ValueError('Missing current context layer')
        if layer.get('current_project_id') != PROJECT or layer.get('owner') != 'ASHLEY' or layer.get('current_gate') != gate:
            raise ValueError('Current layers disagree')
        if layer.get('product_work') != 'FROZEN' or layer.get('auto_resume_projects') is not False:
            raise ValueError('Control Plane requires explicit product freeze')
    if state.get('forbidden_product_execution') is not True:
        raise ValueError('Product boundary is not frozen')
    if PureWindowsPath(state.get('canonical_path', '')) != PureWindowsPath(entry['canonical_path']):
        raise ValueError('Canonical path mismatch')
    policy = state.get('persistent_policy', {}).get('operating_rules', {})
    if policy.get('newer_owner_directive_priority') is not True or policy.get('manual_sigue_required') is not False:
        raise ValueError('Persistent autonomy policy mismatch')
    writers = guard.get('active_writers')
    if not isinstance(writers, list) or len(writers) > 32:
        raise ValueError('Invalid writer reservation registry')
    reserved = []
    for row in writers:
        if not isinstance(row, dict) or row.get('released_at') is not None:
            raise ValueError('Invalid active reservation')
        reserved.append({'holder': _text(row.get('lock_holder')), 'scope': _text(row.get('scope'), 1000)})
    return {'project': PROJECT, 'owner': 'ASHLEY', 'canonical_path': entry['canonical_path'],
            'goal': _text(state.get('active_goal')), 'gate': gate,
            'next_safe_gate': _text(state.get('next_safe_control_plane_gate')),
            'product_work': 'FROZEN', 'factory_health': 'NOT_VERIFIED_BY_THIS_READER',
            'running_workers': 'UNKNOWN_REQUIRES_RUNTIME_PROOF', 'reservations': reserved,
            'source_sha256': {'state': state_sha, 'registry': registry_sha, 'guard': guard_sha},
            'observed_at_utc': datetime.now(timezone.utc).isoformat()}


def render_context(_session_info=None, **_kwargs):
    try:
        view = build_view()
        # No transcript or historical product state is returned. This is a derived
        # observation of declared authority, not a worker/process health attestation.
        text = ('CONTROL_PLANE_CURRENT_CONTEXT_V1\n' + json.dumps(view, ensure_ascii=True, separators=(',', ':')) +
                '\nHermes remains foreman. Reuse existing work; one writer per overlapping scope; writer is not final reviewer. A reservation is not RUNNING proof. Continue safe gates without a manual sigue. Product work is frozen; David is preserve-only. Current runtime/GitHub and the latest owner directive outrank historical chat. Recheck these source hashes before mutation; do not treat this view as a lease or as Factory certification.')
        if len(text) > 3800:
            return FAIL_CLOSED
        return text
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return FAIL_CLOSED


def before_llm(**_kwargs):
    # Reload for each turn, including resumed/reconnected sessions. Native core
    # injects this ephemeral context without rewriting a cached system prompt.
    return {'context': render_context()}


def before_tool(**_kwargs):
    # Unknown authority fails closed. Valid context does not replace OS/tool
    # permissions, task scope guards, or the actual shared writer lease.
    try:
        build_view()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {'action': 'block', 'message': FAIL_CLOSED}
    return None


def register(ctx):
    ctx.register_system_prompt_section('ashley.control-plane.current', render_context, max_chars=4000)
    ctx.register_hook('pre_llm_call', before_llm)
    ctx.register_hook('pre_tool_call', before_tool)
