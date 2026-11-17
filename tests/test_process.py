from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from could_not_dispatch import process
from could_not_dispatch.constants import RUN_TOKEN_ENV

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process handling")
proc_fs_only = pytest.mark.skipif(
    not os.path.isdir("/proc"), reason="requires the /proc filesystem"
)


def _wait_for_token(pid: int, token: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.read_token(pid) == token:
            return True
        time.sleep(0.01)
    return False


def test_prepend_pythonpath_puts_the_plugin_first():
    assert process.prepend_pythonpath(f"/a{os.pathsep}/b", "/plugin") == os.pathsep.join(
        ["/plugin", "/a", "/b"]
    )


def test_prepend_pythonpath_handles_an_empty_value():
    assert process.prepend_pythonpath(None, "/plugin") == "/plugin"
    assert process.prepend_pythonpath("", "/plugin") == "/plugin"


def test_prepend_pythonpath_does_not_duplicate_an_existing_entry():
    assert process.prepend_pythonpath(f"/plugin{os.pathsep}/a", "/plugin") == os.pathsep.join(
        ["/plugin", "/a"]
    )


def test_port_is_free_reports_a_bound_port_as_taken():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert process.port_is_free("127.0.0.1", port) is False
    assert process.port_is_free("127.0.0.1", port) is True


class _SandboxedSocket:
    def __init__(self, real: socket.socket) -> None:
        self._socket = real

    def __getattr__(self, name: str) -> object:
        return getattr(self._socket, name)


class _SandboxedSocketModule:
    def __getattr__(self, name: str) -> object:
        value = getattr(socket, name)
        if name == "socket":
            return lambda *a, **kw: _SandboxedSocket(socket.socket(*a, **kw))
        return value


def test_port_is_free_works_with_the_dispatcharr_socket_sandbox(monkeypatch):
    monkeypatch.setattr(process, "socket", _SandboxedSocketModule())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        assert process.port_is_free("127.0.0.1", port) is False
    assert process.port_is_free("127.0.0.1", port) is True


def test_resolve_interpreter_returns_a_usable_python():
    resolved = process.resolve_interpreter()
    completed = subprocess.run(
        [resolved, "-c", "print(1)"], capture_output=True, timeout=30, check=False
    )
    assert completed.returncode == 0


def test_is_running_rejects_junk_and_impossible_pids():
    assert process.is_running(None) is False
    assert process.is_running("") is False
    assert process.is_running("abc") is False
    assert process.is_running(0) is False
    assert process.is_running(-1) is False


@posix_only
def test_is_running_follows_a_real_process_through_its_life():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process.is_running(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert process.is_running(child.pid) is False


@posix_only
@proc_fs_only
def test_a_process_started_with_another_token_is_left_alone():
    environment = dict(os.environ, **{RUN_TOKEN_ENV: "the-real-one"})
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], env=environment
    )
    try:
        assert _wait_for_token(child.pid, "the-real-one")
        assert process.is_running(child.pid, "the-real-one") is True
        assert process.is_running(child.pid, "a-recycled-pid") is False
        assert process.terminate(child.pid, "a-recycled-pid") is False
        time.sleep(0.2)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)


@posix_only
@proc_fs_only
def test_terminate_stops_the_process_that_carries_the_token():
    environment = dict(os.environ, **{RUN_TOKEN_ENV: "mine"})
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=environment,
        start_new_session=True,
    )
    try:
        assert _wait_for_token(child.pid, "mine")
        assert process.terminate(child.pid, "mine") is True
        assert process.is_running(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@posix_only
def test_terminate_reports_nothing_to_do_for_a_dead_process():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert process.terminate(child.pid) is False


@posix_only
@proc_fs_only
def test_read_token_returns_none_for_a_process_without_one():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={k: v for k, v in os.environ.items() if k != RUN_TOKEN_ENV},
    )
    try:
        assert process.read_token(child.pid) is None
    finally:
        child.kill()
        child.wait(timeout=10)


@posix_only
@proc_fs_only
def test_a_stray_fallback_is_found_by_its_port_even_without_its_token():
    package = "could_not_dispatch.server"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "--port", "19721"],
        env={k: v for k, v in os.environ.items() if k != RUN_TOKEN_ENV},
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child.pid not in process.find_servers(19721):
            time.sleep(0.05)
        assert process.find_servers(19721) == []
    finally:
        child.kill()
        child.wait(timeout=10)
    assert package  # the stray above lacks the module name, so it must not be matched


@posix_only
@proc_fs_only
def test_find_servers_matches_the_module_and_the_exact_port(tmp_path):
    package = tmp_path / "could_not_dispatch"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text("import time\ntime.sleep(30)\n")

    pid = process.spawn(
        tmp_path, ["--port", "19722"], "tok", tmp_path / "log" / "f.log"
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and pid not in process.find_servers(19722):
            time.sleep(0.05)
        assert process.find_servers(19722) == [pid]
        assert process.find_servers(1972) == []
        assert process.find_servers(197220) == []
    finally:
        process.terminate(pid)
    assert process.is_running(pid) is False


@posix_only
@proc_fs_only
def test_terminate_strays_clears_a_process_whose_token_was_lost(tmp_path):
    package = tmp_path / "could_not_dispatch"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text("import time\ntime.sleep(30)\n")

    pid = process.spawn(
        tmp_path, ["--port", "19723"], "the-old-token", tmp_path / "log" / "f.log"
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and pid not in process.find_servers(19723):
        time.sleep(0.05)

    assert process.terminate(pid, "a-token-nobody-recorded") is False
    assert process.is_running(pid) is True

    assert process.terminate_strays(19723) == 1
    assert process.is_running(pid) is False


@posix_only
@proc_fs_only
def test_terminate_strays_spares_the_process_it_is_told_to_keep(tmp_path):
    package = tmp_path / "could_not_dispatch"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text("import time\ntime.sleep(30)\n")

    pid = process.spawn(tmp_path, ["--port", "19724"], "tok", tmp_path / "log" / "f.log")
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and pid not in process.find_servers(19724):
            time.sleep(0.05)
        assert process.terminate_strays(19724, keep_pid=pid) == 0
        assert process.is_running(pid) is True
    finally:
        process.terminate(pid)


@posix_only
def test_spawn_starts_the_server_module_and_it_can_be_stopped(tmp_path):
    package = tmp_path / "could_not_dispatch"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text(
        "import sys, time\n"
        "sys.stderr.write('up\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n"
    )
    log_path = tmp_path / "logs" / "fallback.log"

    pid = process.spawn(tmp_path, ["--media", "x"], "token-123", log_path)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not log_path.exists():
            time.sleep(0.05)
        assert process.is_running(pid, "token-123") is True
        assert log_path.exists()
    finally:
        process.terminate(pid, "token-123")
    assert process.is_running(pid) is False


def test_spawn_hands_the_extra_environment_to_the_server(tmp_path):
    package = tmp_path / "could_not_dispatch"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "server.py").write_text(
        "import os, sys\n"
        "sys.stderr.write('key=' + os.environ.get('COULD_NOT_DISPATCH_API_KEY', '') + '\\n')\n"
    )
    log_path = tmp_path / "logs" / "fallback.log"

    process.spawn(
        tmp_path, [], "token-env", log_path, extra_env={"COULD_NOT_DISPATCH_API_KEY": "s3cret"}
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "key=" not in _read(log_path):
        time.sleep(0.05)
    assert "key=s3cret" in _read(log_path)


def _read(path):
    try:
        return path.read_text()
    except OSError:
        return ""


def _wait_until(check, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@proc_fs_only
def test_a_finished_child_nobody_reaped_counts_as_gone():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        assert _wait_until(lambda: process.is_zombie(child.pid))
        os.kill(child.pid, 0)
    finally:
        child.wait()


@proc_fs_only
def test_a_live_process_is_not_a_zombie():
    assert process.is_zombie(os.getpid()) is False


def test_an_unknown_pid_is_not_a_zombie():
    assert process.is_zombie(2**22 + 7) is False


@proc_fs_only
@pytest.mark.parametrize("close_from", [True, False])
def test_the_spawned_watcher_inherits_no_descriptor_of_the_caller(
    tmp_path: Path, monkeypatch, close_from
):
    if not close_from:
        monkeypatch.delattr(os, "POSIX_SPAWN_CLOSEFROM", raising=False)
    elif not hasattr(os, "POSIX_SPAWN_CLOSEFROM"):
        pytest.skip("POSIX_SPAWN_CLOSEFROM needs Python 3.13")
    (tmp_path / "sleeper.py").write_text(
        "import time\nprint('ready', flush=True)\ntime.sleep(30)\n", encoding="utf-8"
    )
    monkeypatch.setattr(process, "SERVER_MODULE", "sleeper")
    reader, writer = os.pipe()
    os.set_inheritable(reader, True)
    os.set_inheritable(writer, True)
    log = tmp_path / "watcher.log"
    pid = process.spawn(tmp_path, [], "token", log)
    try:
        assert _wait_until(lambda: log.is_file() and "ready" in log.read_text())
        assert set(os.listdir(f"/proc/{pid}/fd")) == {"0", "1", "2"}
        assert os.readlink(f"/proc/{pid}/fd/1") == str(log)
        assert os.readlink(f"/proc/{pid}/fd/2") == str(log)
        assert os.getsid(pid) == pid
    finally:
        os.close(reader)
        os.close(writer)
        process.terminate(pid, "token")


@proc_fs_only
def test_spawn_returns_once_the_new_process_carries_its_token(tmp_path: Path, monkeypatch):
    (tmp_path / "sleeper.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(process, "SERVER_MODULE", "sleeper")
    for round_number in range(10):
        token = f"token-{round_number}"
        pid = process.spawn(tmp_path, [], token, tmp_path / "watcher.log")
        try:
            assert process.read_token(pid) == token
        finally:
            process.terminate(pid, token)


def test_the_spawn_actions_redirect_the_three_standard_streams(tmp_path: Path):
    if sys.platform == "win32":
        assert process.spawn_actions(tmp_path / "w.log") == []
        return
    actions = process.spawn_actions(tmp_path / "w.log")
    assert actions[0][:3] == (os.POSIX_SPAWN_OPEN, 0, os.devnull)
    assert actions[1][:3] == (os.POSIX_SPAWN_OPEN, 1, str(tmp_path / "w.log"))
    assert actions[2] == (os.POSIX_SPAWN_DUP2, 1, 2)
