"""Isolated context reader and native hook API tests; no model or production writes."""
from pathlib import Path
import copy, importlib.util, json, os
from types import SimpleNamespace
from unittest.mock import patch
import pytest

SRC = Path(os.environ.get('CP_CONTEXT_SOURCE') or (Path(__file__).resolve().parents[1] / 'plugins/ashley-control-plane-context/__init__.py'))
spec=importlib.util.spec_from_file_location('context_candidate',SRC);cp=importlib.util.module_from_spec(spec);spec.loader.exec_module(cp)

@pytest.fixture
def context(tmp_path,monkeypatch):
    state={'current_project_id':cp.PROJECT,'owner':'ASHLEY','canonical_path':'C:/hermes-server','current_gate':'GATE_A','active_goal':'CONTROL_PLANE_REPAIR','next_safe_control_plane_gate':'GATE_B','product_work':'FROZEN','auto_resume_projects':False,'forbidden_product_execution':True,'persistent_policy':{'operating_rules':{'newer_owner_directive_priority':True,'manual_sigue_required':False}}}
    state['session_bootstrap']=copy.deepcopy(state);state['control_plane']=copy.deepcopy(state)
    registry={'projects':[{'project_id':cp.PROJECT,'logical_owner':'ASHLEY','ashley_authority':'CANONICAL','canonical_path':'C:/hermes-server'}]}
    guard={'active_writers':[]}
    files=[tmp_path/n for n in ['state.json','registry.json','guard.json']]
    for key,p,data in zip(['STATE','REGISTRY','GUARD'],files,[state,registry,guard]):
        p.write_text(json.dumps(data),encoding='utf-8');monkeypatch.setattr(cp,key,p)
    return state,registry,guard,files


def test_canonical_view_and_no_fabricated_running(context):
    view=cp.build_view();assert view['project']==cp.PROJECT
    assert view['running_workers'].startswith('UNKNOWN')
    assert view['factory_health']=='NOT_VERIFIED_BY_THIS_READER'
    assert len(view['source_sha256']['state'])==64
    assert cp.before_tool() is None


@pytest.mark.parametrize('bad',['owner','project','gate','freeze','policy','missing','registry_owner','registry_duplicate','writers','canonical_path'])
def test_invalid_authority_blocks_without_historical_fallback(context,bad):
    s,r,g,files=context
    if bad=='owner':s['owner']='DAVID'
    elif bad=='project':s['session_bootstrap']['current_project_id']='suini'
    elif bad=='gate':s['control_plane']['current_gate']='OLD_GATE'
    elif bad=='freeze':s['auto_resume_projects']=True
    elif bad=='policy':s['persistent_policy']['operating_rules']['manual_sigue_required']=True
    elif bad=='missing':s.pop('session_bootstrap')
    elif bad=='registry_owner':r['projects'][0]['logical_owner']='DAVID'
    elif bad=='registry_duplicate':r['projects']*=2
    elif bad=='writers':g['active_writers']='invalid'
    elif bad=='canonical_path':s['canonical_path']='C:/foreign'
    for p,v in zip(files,[s,r,g]):p.write_text(json.dumps(v),encoding='utf-8')
    assert cp.render_context()==cp.FAIL_CLOSED
    assert cp.before_tool()['action']=='block'
    assert cp.before_llm()['context']==cp.FAIL_CLOSED


@pytest.mark.parametrize('raw',['{','[]','{"owner":"ASHLEY","owner":"DAVID"}',None])
def test_malformed_duplicate_missing_input_fails_closed(context,raw):
    p=context[3][0]
    if raw is None:p.unlink()
    else:p.write_text(raw,encoding='utf-8')
    assert cp.render_context()==cp.FAIL_CLOSED
    assert cp.before_tool()['action']=='block'


def test_oversized_input_fails_closed(context,monkeypatch):
    monkeypatch.setattr(cp,'MAX_BYTES',5)
    assert cp.render_context()==cp.FAIL_CLOSED


def test_reservations_are_not_running_proof(context):
    s,r,g,files=context;g['active_writers']=[{'lock_holder':'writer-x','scope':'C:/isolated','released_at':None}]
    files[2].write_text(json.dumps(g),encoding='utf-8')
    view=cp.build_view();assert view['reservations'][0]['holder']=='writer-x'
    assert view['running_workers'].startswith('UNKNOWN')


def test_each_turn_reads_current_state_without_mutation(context):
    s,r,g,files=context
    before={p:p.read_bytes() for p in files}
    with patch.object(Path,'write_text',side_effect=AssertionError('writer forbidden')), patch.object(Path,'write_bytes',side_effect=AssertionError('writer forbidden')), patch('socket.socket',side_effect=AssertionError('network forbidden')):
        assert 'GATE_A' in cp.before_llm()['context']
    assert all(p.read_bytes()==v for p,v in before.items())
    for layer in (s,s['session_bootstrap'],s['control_plane']):layer['current_gate']='GATE_NEW'
    files[0].write_text(json.dumps(s),encoding='utf-8')
    assert 'GATE_NEW' in cp.before_llm()['context']
    assert 'GATE_A' not in cp.before_llm()['context']


def test_native_registration_and_reconnect_context_without_prompt_rewrite(context,monkeypatch,tmp_path):
    from hermes_cli import plugins
    from agent.turn_context import _collect_pre_llm_call_context
    manager=plugins.PluginManager(str(tmp_path/'home'))
    manifest=plugins.PluginManifest(name='ashley-control-plane-context',version='1.0.0')
    cp.register(plugins.PluginContext(manifest,manager))
    manager._discovered=True  # isolated registrations only, not unrelated providers
    monkeypatch.setattr(plugins,'_ensure_plugins_discovered',lambda *a,**k:manager)
    monkeypatch.setattr(plugins,'get_plugin_manager',lambda:manager)
    sections=manager.render_system_prompt_sections({'session_id':'fresh-fixture'})
    assert len(sections)==1 and 'GATE_A' in sections[0].content
    frozen=sections[0].content
    agent=SimpleNamespace(session_id='fixture',model='no-inference',platform='cli',_plugin_system_prompt_sections_snapshot=tuple(sections))
    def turn(history):return _collect_pre_llm_call_context(agent,effective_task_id='',turn_id='turn',original_user_message='status',messages=[],conversation_history=history)
    assert 'GATE_A' in turn(None)  # fresh
    s,r,g,files=context
    for layer in (s,s['session_bootstrap'],s['control_plane']):layer['current_gate']='GATE_RESUMED'
    files[0].write_text(json.dumps(s),encoding='utf-8')
    assert 'GATE_RESUMED' in turn([{'role':'user','content':'previous'}])
    assert sections[0].content==frozen and agent._plugin_system_prompt_sections_snapshot[0].content==frozen
    assert 'GATE_RESUMED' in manager.render_system_prompt_sections({'session_id':'reset-fixture'})[0].content
    files[0].write_text('{',encoding='utf-8')
    blocked,_=plugins._dispatch_pre_tool_call_hooks('terminal',{'command':'echo safe'},session_id='fixture')
    assert blocked and 'UNVERIFIED' in blocked
