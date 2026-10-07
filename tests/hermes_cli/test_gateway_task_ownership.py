"""Windows task selection and real launcher lifetime/exit-code contracts."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from hermes_cli import gateway_windows

pytestmark = pytest.mark.platforms('windows')


@pytest.mark.parametrize('name', ['Hermes_Gateway_Ashley', 'Hermes-Research_2'])
def test_explicit_task_name_selects_this_owner_without_changing_default(monkeypatch, name):
    from hermes_cli import config, gateway
    monkeypatch.setattr(gateway, '_profile_suffix', lambda: '')
    monkeypatch.setattr(config, 'load_config', lambda: {'gateway': {'windows_task_name': name}})
    assert gateway_windows.get_task_name() == name
    monkeypatch.setattr(config, 'load_config', lambda: {'gateway': {}})
    assert gateway_windows.get_task_name() == gateway_windows._TASK_NAME_DEFAULT


@pytest.mark.parametrize('invalid', ['', ' ', 'Other\\Task', '../Other', 7, False])
def test_invalid_explicit_task_name_never_falls_back_to_another_owner(monkeypatch, invalid):
    from hermes_cli import config
    monkeypatch.setattr(config, 'load_config', lambda: {'gateway': {'windows_task_name': invalid}})
    with pytest.raises(ValueError, match='windows_task_name'):
        gateway_windows.get_task_name()


@pytest.mark.integration
@pytest.mark.parametrize('exit_code', [0, 75])
def test_vbs_supervisor_waits_for_child_and_propagates_exit(tmp_path, monkeypatch, exit_code):
    ready, release, ended = [tmp_path / name for name in ('ready.json', 'release', 'ended')]
    # Keep the production renderer and native cscript/Windows process behavior;
    # substitute only its child command with a bounded signaling interpreter.
    code = ('import os,json,time,sys;from pathlib import Path;'
            'ready,release,ended=map(Path,sys.argv[1:4]);'
            'ready.write_text(json.dumps({"pid":os.getpid()}));'
            'deadline=time.monotonic()+30;'
            '\nwhile not release.exists() and time.monotonic()<deadline: time.sleep(.05)\n'
            'ended.write_text("exited");sys.exit(int(sys.argv[4]))')
    child_script = tmp_path / 'child.py'
    child_script.write_text(code, encoding='utf-8')
    argv = [sys.executable, str(child_script), str(ready), str(release), str(ended), str(exit_code)]
    monkeypatch.setattr(gateway_windows, '_gateway_run_argv', lambda *args: argv)
    vbs = tmp_path / 'gateway.vbs'
    vbs.write_text(gateway_windows._build_gateway_vbs_script(
        sys.executable, str(tmp_path), str(tmp_path / 'home'), ''), encoding='utf-8')
    process = subprocess.Popen(['cscript.exe', '//Nologo', str(vbs)], cwd=tmp_path,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists(), 'The real child did not start'
        assert json.loads(ready.read_text(encoding='utf-8-sig'))['pid'] != process.pid
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        release.touch()
        out, err = process.communicate(timeout=15)
        assert process.returncode == exit_code, (out, err)
        assert ended.exists()
    finally:
        release.touch(exist_ok=True)
        process.communicate(timeout=15)
        deadline = time.monotonic() + 15
        while ready.exists() and not ended.exists() and time.monotonic() < deadline:
            time.sleep(.05)
