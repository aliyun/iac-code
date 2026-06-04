from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from threading import Event

import httpx
import yaml

from iac_code.services import update_checker
from iac_code.services.update_checker import (
    CONFIGURED_PIP_SOURCE,
    DEFAULT_RELEASE_NOTES_URL,
    OFFICIAL_PYPI_SOURCE,
    PendingUpdate,
    UpdateState,
    check_for_updates_once,
    get_pending_update,
    get_update_state_path,
    load_update_state,
    run_update_command,
    start_background_update_check,
    suppress_version,
)


def _pending_update_data() -> dict[str, object]:
    return {
        "version": "0.4.0",
        "current_version": "0.3.0",
        "source": "official_pypi",
        "checked_at": 100.0,
        "update_command": ["/python", "-m", "pip", "install", "--upgrade", "iac-code"],
        "release_notes_url": "https://github.com/aliyun/iac-code/releases/latest",
    }


class UnexpectedHTTPCall(BaseException):
    pass


class FakeHTTPClient:
    def __init__(self, *results: httpx.Response | BaseException) -> None:
        self.results = list(results)
        self.urls: list[str] = []

    def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        if not self.results:
            raise UnexpectedHTTPCall(f"No queued HTTP response for {url}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _json_response(url: str, payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("GET", url))


def test_update_state_path_follows_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path))
    assert get_update_state_path() == tmp_path.resolve() / "update-state.yml"


def test_load_missing_state_returns_empty(tmp_path):
    state = load_update_state(tmp_path / "missing.yml")
    assert state == UpdateState()


def test_load_corrupt_state_returns_empty(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text("not: valid: yaml: [", encoding="utf-8")
    state = load_update_state(path)
    assert state == UpdateState()


def test_load_non_mapping_state_returns_empty(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
    state = load_update_state(path)
    assert state == UpdateState()


def test_load_pending_update_from_yaml(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": _pending_update_data(),
                "last_successful_check_at": 100.0,
                "skip_until_version": "0.5.0",
            }
        ),
        encoding="utf-8",
    )
    state = load_update_state(path)
    assert state.pending == PendingUpdate(
        version="0.4.0",
        current_version="0.3.0",
        source="official_pypi",
        checked_at=100.0,
        update_command=("/python", "-m", "pip", "install", "--upgrade", "iac-code"),
        release_notes_url="https://github.com/aliyun/iac-code/releases/latest",
    )
    assert state.pending.update_command == ("/python", "-m", "pip", "install", "--upgrade", "iac-code")
    assert state.last_successful_check_at == 100.0
    assert state.skip_until_version == "0.5.0"


def test_pending_update_converts_update_command_to_tuple():
    update = PendingUpdate(
        version="0.4.0",
        current_version="0.3.0",
        source="official_pypi",
        checked_at=100.0,
        update_command=["/python", "-m", "pip"],
    )

    assert update.update_command == ("/python", "-m", "pip")


def test_run_update_command_uses_pending_command():
    pending = PendingUpdate(
        version="0.4.0",
        current_version="0.3.0",
        source=OFFICIAL_PYPI_SOURCE,
        checked_at=100.0,
        update_command=("/python", "-m", "pip", "install", "--upgrade", "iac-code"),
    )
    calls = []
    expected = subprocess.CompletedProcess(args=pending.update_command, returncode=0)

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    result = run_update_command(pending, subprocess_run=fake_run)

    assert result is expected
    assert calls == [
        (
            (pending.update_command,),
            {
                "text": True,
                "stdout": None,
                "stderr": None,
                "check": False,
            },
        )
    ]


def test_start_background_update_check_runs_check_without_blocking(tmp_path):
    worker_started = Event()
    release_worker = Event()
    calls = []

    def fake_check_func(**kwargs):
        calls.append(kwargs)
        worker_started.set()
        release_worker.wait(timeout=1.0)
        return UpdateState()

    thread = start_background_update_check(
        path=tmp_path / "update-state.yml",
        current_version="0.3.0",
        release_date="2026-05-01",
        python_executable="/python",
        check_func=fake_check_func,
    )

    assert thread.name == "iac-code-update-checker"
    assert thread.daemon
    assert worker_started.wait(timeout=1.0)
    assert thread.is_alive()

    release_worker.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert calls == [
        {
            "path": tmp_path / "update-state.yml",
            "current_version": "0.3.0",
            "release_date": "2026-05-01",
            "python_executable": "/python",
        }
    ]


def test_start_background_update_check_swallows_and_logs_exceptions(caplog):
    def fake_check_func(**kwargs):
        raise RuntimeError("boom")

    with caplog.at_level(logging.DEBUG, logger=update_checker.__name__):
        thread = start_background_update_check(check_func=fake_check_func)
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert "Background update check failed" in caplog.text


def test_get_pending_update_ignores_not_newer_version(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    "version": "0.3.0",
                    "current_version": "0.2.0",
                    "source": "configured_pip",
                    "checked_at": 100.0,
                    "update_command": ["/python", "-m", "pip", "install", "--upgrade", "iac-code"],
                    "release_notes_url": "https://github.com/aliyun/iac-code/releases/latest",
                }
            }
        ),
        encoding="utf-8",
    )
    assert get_pending_update(path=path, current_version="0.3.0") is None


def test_get_pending_update_defaults_to_running_version(monkeypatch, tmp_path):
    import iac_code

    path = tmp_path / "update-state.yml"
    pending = {**_pending_update_data(), "version": "0.4.0", "current_version": "0.3.0"}
    path.write_text(yaml.safe_dump({"pending": pending}), encoding="utf-8")
    monkeypatch.setattr(iac_code, "__version__", "0.4.0")

    assert get_pending_update(path=path) is None


def test_get_pending_update_honors_skip_until_version(tmp_path):
    path = tmp_path / "update-state.yml"
    pending = _pending_update_data()
    path.write_text(yaml.safe_dump({"pending": pending, "skip_until_version": "0.4.0"}), encoding="utf-8")
    assert get_pending_update(path=path, current_version="0.3.0") is None


def test_suppress_version_merges_with_existing_pending(tmp_path):
    path = tmp_path / "update-state.yml"
    pending = _pending_update_data()
    path.write_text(yaml.safe_dump({"pending": pending}), encoding="utf-8")

    suppress_version("0.4.0", path=path)

    state = load_update_state(path)
    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.skip_until_version == "0.4.0"


def test_suppress_version_preserves_last_successful_check_at(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump({"pending": _pending_update_data(), "last_successful_check_at": 100.0}),
        encoding="utf-8",
    )

    suppress_version("0.4.0", path=path)

    state = load_update_state(path)
    assert state.last_successful_check_at == 100.0
    assert state.skip_until_version == "0.4.0"


def test_suppress_version_writes_valid_final_yaml_without_partial_temp_file(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"pending": _pending_update_data()}), encoding="utf-8")

    suppress_version("0.4.0", path=path)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["pending"]["version"] == "0.4.0"
    assert data["pending"]["update_command"] == ["/python", "-m", "pip", "install", "--upgrade", "iac-code"]
    assert data["skip_until_version"] == "0.4.0"
    assert not list(tmp_path.glob(".update-state.yml.*.tmp"))


def test_suppress_version_uses_same_dir_temp_file_fsync_and_replace(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"pending": _pending_update_data()}), encoding="utf-8")
    original_mkstemp = update_checker.tempfile.mkstemp
    original_fsync = update_checker.os.fsync
    original_replace = update_checker.os.replace
    mkstemp_calls = []
    fsync_calls = []
    replace_calls = []

    def spy_mkstemp(*, prefix, suffix, dir):
        mkstemp_calls.append({"prefix": prefix, "suffix": suffix, "dir": dir})
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    def spy_fsync(fd):
        fsync_calls.append(fd)
        original_fsync(fd)

    def spy_replace(src, dst):
        replace_calls.append((src, dst))
        original_replace(src, dst)

    monkeypatch.setattr(update_checker.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(update_checker.os, "fsync", spy_fsync)
    monkeypatch.setattr(update_checker.os, "replace", spy_replace)

    suppress_version("0.4.0", path=path)

    assert mkstemp_calls == [{"prefix": ".update-state.yml.", "suffix": ".tmp", "dir": tmp_path}]
    assert fsync_calls
    assert len(replace_calls) == 1
    temp_path, final_path = replace_calls[0]
    assert Path(temp_path).parent == tmp_path
    assert final_path == path


def test_suppress_version_writes_when_advisory_lock_acquire_fails(monkeypatch, tmp_path):
    class BrokenFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(_fileno, _operation):
            raise OSError("lock unavailable")

    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"pending": _pending_update_data()}), encoding="utf-8")
    monkeypatch.setattr(update_checker, "_fcntl", BrokenFcntl)

    suppress_version("0.4.0", path=path)

    state = load_update_state(path)
    assert state.skip_until_version == "0.4.0"


def test_suppress_version_writes_when_advisory_lock_release_fails(monkeypatch, tmp_path):
    class BrokenUnlockFcntl:
        LOCK_EX = 1
        LOCK_UN = 2
        operations = []

        @classmethod
        def flock(cls, _fileno, operation):
            cls.operations.append(operation)
            if operation == cls.LOCK_UN:
                raise OSError("unlock unavailable")

    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"pending": _pending_update_data()}), encoding="utf-8")
    monkeypatch.setattr(update_checker, "_fcntl", BrokenUnlockFcntl)

    suppress_version("0.4.0", path=path)

    state = load_update_state(path)
    assert state.skip_until_version == "0.4.0"
    assert BrokenUnlockFcntl.operations == [BrokenUnlockFcntl.LOCK_EX, BrokenUnlockFcntl.LOCK_UN]


def test_suppress_version_writes_when_advisory_lock_file_open_fails(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    lock_path = tmp_path / ".update-state.yml.lock"
    path.write_text(yaml.safe_dump({"pending": _pending_update_data()}), encoding="utf-8")
    original_open = Path.open

    def open_with_lock_failure(self, *args, **kwargs):
        if self == lock_path:
            raise OSError("lock file unavailable")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_lock_failure)

    suppress_version("0.4.0", path=path)

    state = load_update_state(path)
    assert state.skip_until_version == "0.4.0"


def test_official_pypi_wins_over_configured_pip(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.pending.source == OFFICIAL_PYPI_SOURCE
    assert list(state.pending.update_command) == [
        "/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--index-url",
        "https://pypi.org/simple",
        "iac-code",
    ]
    assert state.pending.release_notes_url == "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"
    assert http_client.urls == [
        "https://pypi.org/pypi/iac-code/json",
        "https://api.github.com/repos/aliyun/iac-code/releases/latest",
    ]


def test_official_failure_falls_back_to_configured_pip(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        httpx.ConnectError("pypi unavailable"),
        httpx.ConnectError("github unavailable"),
    )
    run_calls = []

    def fake_run(*args, **kwargs):
        run_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.3.2)\nAvailable versions: 0.3.2, 0.3.1, 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.3.2"
    assert state.pending.source == CONFIGURED_PIP_SOURCE
    assert list(state.pending.update_command) == ["/python", "-m", "pip", "install", "--upgrade", "iac-code"]
    assert state.pending.release_notes_url == DEFAULT_RELEASE_NOTES_URL
    assert run_calls == [
        (
            (["/python", "-m", "pip", "index", "versions", "iac-code", "--disable-pip-version-check"],),
            {"capture_output": True, "text": True, "timeout": 30, "check": False},
        )
    ]


def test_official_success_without_update_falls_back_to_configured_pip(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.3.0": [{}], "0.2.9": [{}]}}),
        httpx.ConnectError("github unavailable"),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.3.1)\nAvailable versions: 100.0.0, 0.3.1, 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "100.0.0"
    assert state.pending.source == CONFIGURED_PIP_SOURCE
    assert state.last_successful_check_at == 1000.0


def test_successful_check_within_two_hours_is_throttled(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"last_successful_check_at": 1000.0}), encoding="utf-8")
    http_client = FakeHTTPClient()

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(path=path, http_client=http_client, now=8199.0, python_executable="/python")

    assert state == UpdateState(last_successful_check_at=1000.0)
    assert load_update_state(path) == UpdateState(last_successful_check_at=1000.0)
    assert http_client.urls == []


def test_force_check_ignores_throttle_for_manual_update(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"last_successful_check_at": 1000.0}), encoding="utf-8")
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=8199.0,
        python_executable="/python",
        force=True,
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.last_successful_check_at == 8199.0
    assert http_client.urls == [
        "https://pypi.org/pypi/iac-code/json",
        "https://api.github.com/repos/aliyun/iac-code/releases/latest",
    ]


def test_failed_check_does_not_update_last_successful_check_at(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        httpx.ConnectError("pypi unavailable"),
        httpx.ConnectError("github unavailable"),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="no matching distribution")

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is None
    assert state.last_successful_check_at is None
    assert load_update_state(path).last_successful_check_at is None


def test_failed_check_is_retried_on_next_startup(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    first_http_client = FakeHTTPClient(httpx.ConnectError("pypi unavailable"))

    def failed_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="no matching distribution")

    monkeypatch.setattr(update_checker.subprocess, "run", failed_run)

    check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=first_http_client,
        now=1000.0,
        python_executable="/python",
    )

    second_http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    second_state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=second_http_client,
        now=1001.0,
        python_executable="/python",
    )

    assert second_state.pending is not None
    assert second_state.pending.version == "0.4.0"
    assert second_http_client.urls == [
        "https://pypi.org/pypi/iac-code/json",
        "https://api.github.com/repos/aliyun/iac-code/releases/latest",
    ]


def test_prerelease_installed_version_accepts_newer_prerelease_target(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response(
            "https://pypi.org/pypi/iac-code/json",
            {"releases": {"0.4.0b2": [{}], "0.4.0b1": [{}], "0.3.0": [{}]}},
        ),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0b2"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.4.0b1",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0b2"
    assert state.pending.source == OFFICIAL_PYPI_SOURCE


def test_stable_installed_version_ignores_prerelease_target(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0b1": [{}], "0.3.0": [{}]}}),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.4.0b1)\nAvailable versions: 0.4.0b1, 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is None
    assert state.last_successful_check_at == 1000.0


def test_local_development_build_skips_detection(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient()

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
        release_date="",
    )

    assert state == UpdateState()
    assert load_update_state(path) == UpdateState()
    assert http_client.urls == []


def test_force_check_runs_for_manual_update_from_local_development_build(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
        release_date="",
        force=True,
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.last_successful_check_at == 1000.0
    assert http_client.urls == [
        "https://pypi.org/pypi/iac-code/json",
        "https://api.github.com/repos/aliyun/iac-code/releases/latest",
    ]


def test_newer_existing_pending_is_preserved_when_detector_races(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.5.0",
                    "checked_at": 1001.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.5.0"
    assert load_update_state(path).pending.version == "0.5.0"


def test_semantically_better_detected_pending_wins_over_newer_race_pending(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.4.0",
                    "checked_at": 1001.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.5.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.5.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(path=path, http_client=http_client, now=1000.0, python_executable="/python")

    assert state.pending is not None
    assert state.pending.version == "0.5.0"
    assert state.pending.checked_at == 1000.0
    assert load_update_state(path).pending.version == "0.5.0"


def test_newer_configured_race_pending_wins_over_older_official_detection(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.5.0",
                    "source": CONFIGURED_PIP_SOURCE,
                    "checked_at": 1001.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(path=path, http_client=http_client, now=1000.0, python_executable="/python")

    assert state.pending is not None
    assert state.pending.version == "0.5.0"
    assert state.pending.source == CONFIGURED_PIP_SOURCE
    assert state.pending.checked_at == 1001.0


def test_stale_official_pending_does_not_hide_fresh_configured_update(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.4.0",
                    "source": OFFICIAL_PYPI_SOURCE,
                    "checked_at": 900.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        httpx.ConnectError("pypi unavailable"),
        httpx.ConnectError("github unavailable"),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.6.0)\nAvailable versions: 0.6.0, 0.5.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.5.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.6.0"
    assert state.pending.source == CONFIGURED_PIP_SOURCE


def test_newer_configured_pending_is_preserved_against_older_fresh_official_detection(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.5.0",
                    "source": CONFIGURED_PIP_SOURCE,
                    "checked_at": 900.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.4.0": [{}], "0.3.0": [{}]}}),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(path=path, http_client=http_client, now=1000.0, python_executable="/python")

    assert state.pending is not None
    assert state.pending.version == "0.5.0"
    assert state.pending.source == CONFIGURED_PIP_SOURCE
    assert load_update_state(path).pending.version == "0.5.0"


def test_official_pypi_ignores_empty_and_yanked_releases(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response(
            "https://pypi.org/pypi/iac-code/json",
            {
                "releases": {
                    "0.5.0": [],
                    "0.4.0": [{"yanked": True}, {"yanked": True}],
                    "0.3.2": [{"yanked": False}],
                    "0.3.0": [{}],
                }
            },
        ),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.3.2"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.3.2"
    assert state.pending.source == OFFICIAL_PYPI_SOURCE


def test_official_pypi_without_installable_newer_release_falls_back_to_configured_pip(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response(
            "https://pypi.org/pypi/iac-code/json",
            {
                "releases": {
                    "0.5.0": [],
                    "0.4.0": [{"yanked": True}],
                    "0.3.0": [{}],
                }
            },
        ),
        httpx.ConnectError("github unavailable"),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.3.1)\nAvailable versions:100.0.0, 0.3.1, 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(path=path, http_client=http_client, now=1000.0, python_executable="/python")

    assert state.pending is not None
    assert state.pending.version == "100.0.0"
    assert state.pending.source == CONFIGURED_PIP_SOURCE
    assert state.pending.release_notes_url == DEFAULT_RELEASE_NOTES_URL


def test_official_pypi_skips_files_incompatible_with_current_python(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response(
            "https://pypi.org/pypi/iac-code/json",
            {
                "releases": {
                    "0.5.0": [{"requires_python": ">=3.12"}],
                    "0.4.0": [{"requires_python": ">=3.10"}],
                    "0.3.0": [{}],
                }
            },
        ),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
        python_version="3.10.0",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.pending.source == OFFICIAL_PYPI_SOURCE


def test_official_pypi_skips_invalid_requires_python_marker(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    http_client = FakeHTTPClient(
        _json_response(
            "https://pypi.org/pypi/iac-code/json",
            {
                "releases": {
                    "0.5.0": [{"requires_python": "not a specifier"}],
                    "0.4.0": [{"requires_python": ""}],
                    "0.3.0": [{}],
                }
            },
        ),
        _json_response(
            "https://api.github.com/repos/aliyun/iac-code/releases/latest",
            {"html_url": "https://github.com/aliyun/iac-code/releases/tag/v0.4.0"},
        ),
    )

    def fail_run(*args, **kwargs):
        raise AssertionError("pip subprocess must not run")

    monkeypatch.setattr(update_checker.subprocess, "run", fail_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
        python_version="3.10.0",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.pending.source == OFFICIAL_PYPI_SOURCE


def test_successful_check_keeps_last_successful_check_at_monotonic(tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(yaml.safe_dump({"last_successful_check_at": 2000.0}), encoding="utf-8")

    state = update_checker._write_detected_state(path, pending=None, checked_at=1000.0, source_success=True)

    assert state.last_successful_check_at == 2000.0


def test_configured_pending_survives_when_configured_source_fails(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.4.0",
                    "source": CONFIGURED_PIP_SOURCE,
                    "checked_at": 900.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.3.0": [{}]}}),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="configured unavailable")

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.pending.source == CONFIGURED_PIP_SOURCE


def test_official_pending_survives_when_official_source_fails(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.4.0",
                    "source": OFFICIAL_PYPI_SOURCE,
                    "checked_at": 900.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(httpx.ConnectError("pypi unavailable"))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.3.0)\nAvailable versions: 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.pending.source == OFFICIAL_PYPI_SOURCE


def test_successful_no_update_check_clears_stale_pending(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.4.0",
                    "source": CONFIGURED_PIP_SOURCE,
                    "checked_at": 900.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.3.0": [{}]}}),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.3.0)\nAvailable versions: 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(path=path, http_client=http_client, now=1000.0, python_executable="/python")

    assert state.pending is None
    assert state.last_successful_check_at == 1000.0
    assert load_update_state(path).pending is None


def test_successful_no_update_check_preserves_newer_race_pending(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": {
                    **_pending_update_data(),
                    "version": "0.4.0",
                    "source": CONFIGURED_PIP_SOURCE,
                    "checked_at": 1001.0,
                }
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(
        _json_response("https://pypi.org/pypi/iac-code/json", {"releases": {"0.3.0": [{}]}}),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="iac-code (0.3.0)\nAvailable versions: 0.3.0\n",
            stderr="",
        )

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=1000.0,
        python_executable="/python",
    )

    assert state.pending is not None
    assert state.pending.version == "0.4.0"
    assert state.pending.checked_at == 1001.0
    assert state.last_successful_check_at == 1000.0


def test_all_update_sources_fail_preserves_existing_state(monkeypatch, tmp_path):
    path = tmp_path / "update-state.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "pending": _pending_update_data(),
                "last_successful_check_at": 500.0,
            }
        ),
        encoding="utf-8",
    )
    http_client = FakeHTTPClient(httpx.ConnectError("pypi unavailable"))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="no matching distribution")

    monkeypatch.setattr(update_checker.subprocess, "run", fake_run)

    state = check_for_updates_once(
        path=path,
        current_version="0.3.0",
        http_client=http_client,
        now=8000.0,
        python_executable="/python",
    )

    assert state.pending == PendingUpdate(**_pending_update_data())
    assert state.last_successful_check_at == 500.0
    assert load_update_state(path) == state
