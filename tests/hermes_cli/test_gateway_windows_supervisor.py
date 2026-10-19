from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import psutil
import pytest

import hermes_cli.gateway_windows_supervisor as supervisor


class _FakeOwner:
    acquired = True
    released = 0

    def __init__(self, _path: Path) -> None:
        pass

    def acquire(self) -> bool:
        return type(self).acquired

    def release(self) -> None:
        type(self).released += 1


def test_repeated_exit_75_is_bounded(monkeypatch, tmp_path):
    """Exit 75 is retryable, not privileged: watchdog/ownership failures also use it."""
    calls = []

    class Proc:
        def __init__(self, argv):
            calls.append(list(argv))

        def wait(self):
            return 75

    _FakeOwner.acquired = True
    _FakeOwner.released = 0
    monkeypatch.setattr(supervisor, "_OwnerLock", _FakeOwner)
    monkeypatch.setattr(supervisor.subprocess, "Popen", Proc)
    monkeypatch.setattr(supervisor, "_ack_stop", lambda *a: False)
    monkeypatch.setattr(supervisor, "_sleep_with_stop", lambda *a: False)

    rc = supervisor.supervise(
        ["python", "-c", "pass"],
        home=tmp_path,
        restart_delay_ms=0,
        failure_window_s=300,
        max_failures=3,
    )

    assert rc == 75
    assert len(calls) == 3
    assert _FakeOwner.released == 1


def test_mixed_retryable_failures_share_one_budget(monkeypatch, tmp_path):
    codes = iter([17, 75, 19])
    calls = []

    class Proc:
        def __init__(self, argv):
            calls.append(list(argv))

        def wait(self):
            return next(codes)

    _FakeOwner.acquired = True
    monkeypatch.setattr(supervisor, "_OwnerLock", _FakeOwner)
    monkeypatch.setattr(supervisor.subprocess, "Popen", Proc)
    monkeypatch.setattr(supervisor, "_ack_stop", lambda *a: False)
    monkeypatch.setattr(supervisor, "_sleep_with_stop", lambda *a: False)

    assert supervisor.supervise(
        ["gateway"], home=tmp_path, restart_delay_ms=0,
        failure_window_s=300, max_failures=3,
    ) == 19
    assert len(calls) == 3


def test_non_owner_never_spawns_or_consumes_stop(monkeypatch, tmp_path):
    _FakeOwner.acquired = False
    monkeypatch.setattr(supervisor, "_OwnerLock", _FakeOwner)
    monkeypatch.setattr(
        supervisor.subprocess, "Popen",
        lambda *a, **k: pytest.fail("non-owner must not spawn a child"),
    )
    monkeypatch.setattr(
        supervisor, "_ack_stop",
        lambda *a: pytest.fail("non-owner must not consume stop state"),
    )

    assert supervisor.supervise(["gateway"], home=tmp_path) == 0


def test_stop_nonce_is_acknowledged_before_marker_is_removed(tmp_path):
    marker, ack, _owner = supervisor._paths(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text("nonce-123\n", encoding="utf-8")

    assert supervisor._ack_stop(marker, ack)
    assert not marker.exists()
    assert ack.read_text(encoding="utf-8") == "nonce-123"


@pytest.mark.platforms("windows")
def test_owner_lock_is_exclusive_and_recoverable(tmp_path):
    """Real Windows byte-range lock: one lifecycle owner, then clean takeover after release."""
    _marker, _ack, lock_path = supervisor._paths(tmp_path)
    first = supervisor._OwnerLock(lock_path)
    second = supervisor._OwnerLock(lock_path)

    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


@pytest.mark.platforms("windows")
@pytest.mark.live_system_guard_bypass  # taskkill targets only the stub child spawned below
def test_tree_kill_restarts_child_without_killing_supervisor(tmp_path):
    """#139914: taskkill /T /F removes the gateway tree while its owner recovers it."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.is_file()
    gateway_code = """
import os
import pathlib
import subprocess
import sys
import threading

home = pathlib.Path(sys.argv[1])
watch_path = home / f"watch-{os.getpid()}.pid"
watch_code = (
    "import os,pathlib,sys,threading; "
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
    "threading.Event().wait()"
)
watch = subprocess.Popen([sys.executable, "-c", watch_code, str(watch_path)])
with (home / "children.txt").open("a", encoding="utf-8") as out:
    out.write(f"{os.getpid()} {watch.pid}\\n")
threading.Event().wait()
"""
    owner = subprocess.Popen(
        [
            str(pythonw), "-m", "hermes_cli.gateway_windows_supervisor",
            "--home", str(tmp_path), "--restart-delay-ms", "100",
            "--failure-window-s", "30", "--max-failures", "3", "--",
            str(pythonw), "-c", gateway_code, str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def generation(count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if owner.poll() is not None:
                pytest.fail(f"supervisor exited before generation {count}: {owner.returncode}")
            rows_path = tmp_path / "children.txt"
            rows = rows_path.read_text(encoding="utf-8").splitlines() if rows_path.exists() else []
            if len(rows) >= count:
                gateway_pid, watch_pid = map(int, rows[count - 1].split())
                if (tmp_path / f"watch-{gateway_pid}.pid").exists():
                    return gateway_pid, watch_pid
            Event().wait(0.05)
        pytest.fail(f"supervisor did not start generation {count}")

    def kill_tree(pid):
        assert owner.pid in {parent.pid for parent in psutil.Process(pid).parents()}
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=15, check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    duplicate = None
    try:
        first, first_watch = generation(1)
        duplicate = subprocess.Popen(
            owner.args,
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "HERMES_HOME": str(tmp_path)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert duplicate.wait(timeout=15) == 0
        assert len((tmp_path / "children.txt").read_text(encoding="utf-8").splitlines()) == 1
        assert owner.poll() is None
        assert psutil.pid_exists(first_watch)
        kill_tree(first)
        second, second_watch = generation(2)
        assert second != first
        assert not psutil.pid_exists(first_watch)
        assert psutil.pid_exists(second_watch)

        marker, ack, _lock = supervisor._paths(tmp_path)
        marker.write_text("planned-stop", encoding="utf-8")
        kill_tree(second)
        assert owner.wait(timeout=15) == 0
        assert ack.read_text(encoding="utf-8") == "planned-stop"
        assert len((tmp_path / "children.txt").read_text(encoding="utf-8").splitlines()) == 2
        assert not psutil.pid_exists(second_watch)
    finally:
        for process in (duplicate, owner):
            if process is None:
                continue
            if process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=15, check=False,
                )
            process.wait(timeout=15)
