import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
from dataclasses import is_dataclass
from pathlib import Path

import pytest
import yaml

from iac_code.pipeline.engine import prerequisites as prereq_module
from iac_code.pipeline.engine.prerequisites import (
    CommandResult,
    PrerequisiteProgress,
    inspect_prerequisites,
    prepare_prerequisites,
)


def _infraguard_prereqs():
    return {
        "infraguard": {
            "command": "infraguard",
            "required_by_flags": ["enable_reviewing"],
            "on_missing": {"repl": "prompt_install", "non_interactive": "disable_feature"},
            "installers": [
                {
                    "id": "homebrew",
                    "platforms": ["darwin", "linux"],
                    "requires_commands": ["brew"],
                    "commands": [
                        ["brew", "tap", "aliyun/infraguard", "https://github.com/aliyun/infraguard"],
                        ["brew", "install", "infraguard"],
                    ],
                },
                {
                    "id": "go-install",
                    "platforms": ["darwin", "linux", "windows"],
                    "requires_commands": ["go"],
                    "env": {"GOPROXY": "https://mirrors.aliyun.com/goproxy/,direct"},
                    "commands": [
                        [
                            "go",
                            "install",
                            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
                        ]
                    ],
                    "path_hints": [
                        {"kind": "command_output", "command": ["go", "env", "GOBIN"]},
                        {"kind": "command_output", "command": ["go", "env", "GOPATH"], "append": "bin"},
                    ],
                },
            ],
            "post_install": {"commands": [["infraguard", "policy", "update"]]},
        }
    }


def _infraguard_prereqs_with_version_check():
    prereqs = _infraguard_prereqs()
    prereqs["infraguard"]["version_check"] = {
        "command": ["infraguard", "version"],
        "minimum": "0.10.1",
        "pattern": r"InfraGuard:\s*(?P<version>\d+\.\d+\.\d+)",
    }
    return prereqs


def _infraguard_prereqs_with_latest_go_install_target():
    prereqs = _infraguard_prereqs()
    go_installer = next(
        installer for installer in prereqs["infraguard"]["installers"] if installer["id"] == "go-install"
    )
    go_installer["commands"] = [
        [
            "go",
            "install",
            {
                "kind": "go-install-latest-github-tag",
                "repo": "aliyun/infraguard",
                "module": "github.com/aliyun/infraguard/cmd/infraguard",
                "tag_prefix": "v",
                "fallback_ref": "0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
            },
        ]
    ]
    return prereqs


def _direct_binary_prereqs(source_url: str, install_dir: str, sha256: str):
    return {
        "infraguard": {
            "command": "infraguard",
            "required_by_flags": ["enable_reviewing"],
            "on_missing": {"repl": "prompt_install", "non_interactive": "disable_feature"},
            "installers": [
                {
                    "id": "direct-binary",
                    "platforms": ["darwin", "linux", "windows"],
                    "download": {
                        "install_dir": install_dir,
                        "installed_name": "infraguard",
                        "assets": [
                            {
                                "platforms": ["darwin"],
                                "architectures": ["arm64"],
                                "filename": "infraguard-v0.10.0-darwin-arm64",
                                "urls": [source_url],
                                "sha256": sha256,
                            }
                        ],
                    },
                }
            ],
            "post_install": {"commands": [["infraguard", "policy", "update"]]},
        }
    }


def test_inspect_disables_required_flag_when_command_missing_and_non_interactive_disables_feature():
    resolution = inspect_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        command_exists=lambda _command: None,
    )

    decision = resolution.decisions["infraguard"]
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert decision.status == "disabled_feature"
    assert decision.required_flags == ["enable_reviewing"]
    assert decision.resolved_path is None


def test_inspect_keeps_flag_enabled_when_command_exists():
    resolution = inspect_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        command_exists=lambda command: f"/opt/bin/{command}",
    )

    decision = resolution.decisions["infraguard"]
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert decision.status == "available"
    assert decision.resolved_path == "/opt/bin/infraguard"


def test_inspect_disables_required_flag_when_existing_command_fails_version_check():
    resolution = inspect_prerequisites(
        _infraguard_prereqs_with_version_check(),
        feature_flags={"enable_reviewing": True},
        command_exists=lambda command: f"/opt/bin/{command}",
        run_command=lambda command, env=None: CommandResult(
            command=command,
            returncode=0,
            stdout="InfraGuard: 0.6.0\n",
            stderr="",
        ),
    )

    decision = resolution.decisions["infraguard"]
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert decision.status == "disabled_feature"
    assert decision.resolved_path == "/opt/bin/infraguard"
    assert "0.6.0" in decision.message


def test_inspect_passes_default_timeout_to_version_check():
    observed_timeouts: list[float | None] = []

    def run_command(command, env=None, timeout_seconds=None):
        observed_timeouts.append(timeout_seconds)
        return CommandResult(command=command, returncode=0, stdout="InfraGuard: 0.10.1\n", stderr="")

    resolution = inspect_prerequisites(
        _infraguard_prereqs_with_version_check(),
        feature_flags={"enable_reviewing": True},
        command_exists=lambda command: f"/opt/bin/{command}",
        run_command=run_command,
    )

    assert resolution.decisions["infraguard"].status == "available"
    assert observed_timeouts == [30.0]


def test_inspect_finds_existing_direct_binary_install_dir_when_not_on_path(tmp_path):
    # Detection must resolve infraguard the same way use-time lookup does: a binary
    # sitting in the installer's install_dir (~/bin) but absent from PATH is available.
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    installed = install_dir / "infraguard"
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    installed.chmod(0o755)

    resolution = inspect_prerequisites(
        _direct_binary_prereqs("https://example.com/infraguard", str(install_dir), "0" * 64),
        feature_flags={"enable_reviewing": True},
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
    )

    decision = resolution.decisions["infraguard"]
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert decision.status == "available"
    assert decision.resolved_path == str(installed)
    assert decision.installer_id == "direct-binary"
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(install_dir)


def test_prepare_offers_only_platform_matching_installers_with_available_required_commands():
    offered_installer_ids = []

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return None

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=lambda command: command == "brew",
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == ["homebrew"]
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_prepare_preserves_installer_display_metadata_for_repl_choice():
    prereqs = _infraguard_prereqs()
    prereqs["infraguard"]["installers"][0]["display_key"] = "homebrew"
    prereqs["infraguard"]["installers"][0]["display_name"] = "Homebrew"
    offered_installers = []

    def choose_installer(_name, installers):
        offered_installers.extend(installers)
        return None

    prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=lambda command: command == "brew",
        choose_installer=choose_installer,
    )

    assert offered_installers[0].display_key == "homebrew"
    assert offered_installers[0].display_name == "Homebrew"


def _infraguard_prereqs_web():
    prereqs = _infraguard_prereqs()
    prereqs["infraguard"]["on_missing"] = {
        "repl": "prompt_install",
        "web": "prompt_install",
        "non_interactive": "disable_feature",
    }
    return prereqs


def test_prepare_web_surface_offers_install_when_on_missing_web_is_prompt_install():
    offered_installer_ids = []

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return None

    resolution = prepare_prerequisites(
        _infraguard_prereqs_web(),
        feature_flags={"enable_reviewing": True},
        surface="web",
        platform_system="linux",
        command_exists=lambda command: command == "brew",
        choose_installer=choose_installer,
    )

    # Gate passed: the installer chooser was reached under surface="web".
    assert offered_installer_ids == ["homebrew"]
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_prepare_web_surface_disables_feature_when_no_web_action_configured():
    reached_chooser = False

    def choose_installer(_name, installers):
        nonlocal reached_chooser
        reached_chooser = True
        return None

    # on_missing has no "web" key -> web must fall back to non_interactive disable.
    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="web",
        platform_system="linux",
        command_exists=lambda command: command == "brew",
        choose_installer=choose_installer,
    )

    assert reached_chooser is False
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "disabled_feature"


def test_prepare_non_interactive_still_disables_even_with_web_prompt_install():
    resolution = prepare_prerequisites(
        _infraguard_prereqs_web(),
        feature_flags={"enable_reviewing": True},
        surface="non_interactive",
        platform_system="linux",
        command_exists=lambda command: command == "brew",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "disabled_feature"


def test_prepare_accepted_install_runs_installer_commands_then_policy_update_and_keeps_review_enabled():
    available_commands = {"brew": "/usr/local/bin/brew"}
    commands_run = []

    def command_exists(command):
        return available_commands.get(command)

    def run_command(command, env=None):
        commands_run.append(command)
        if command == ["brew", "install", "infraguard"]:
            available_commands["infraguard"] = "/usr/local/bin/infraguard"
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "homebrew",
    )

    assert commands_run == [
        ["brew", "tap", "aliyun/infraguard", "https://github.com/aliyun/infraguard"],
        ["brew", "install", "infraguard"],
        ["infraguard", "policy", "update"],
    ]
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].installer_id == "homebrew"


def test_prepare_passes_configured_command_timeouts_to_installer_and_post_install():
    prereqs = _infraguard_prereqs()
    homebrew = prereqs["infraguard"]["installers"][0]
    homebrew["timeout_seconds"] = 321
    prereqs["infraguard"]["post_install"]["timeout_seconds"] = 45
    available_commands = {"brew": "/usr/local/bin/brew"}
    observed_timeouts: list[tuple[list[str], float | None]] = []

    def command_exists(command):
        return available_commands.get(command)

    def run_command(command, env=None, on_output=None, timeout_seconds=None):
        observed_timeouts.append((command, timeout_seconds))
        if command == ["brew", "install", "infraguard"]:
            available_commands["infraguard"] = "/usr/local/bin/infraguard"
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "homebrew",
    )

    assert resolution.decisions["infraguard"].status == "available"
    assert observed_timeouts == [
        (["brew", "tap", "aliyun/infraguard", "https://github.com/aliyun/infraguard"], 321.0),
        (["brew", "install", "infraguard"], 321.0),
        (["infraguard", "policy", "update"], 45.0),
    ]


def test_default_run_command_returns_timeout_result_and_terminates_child():
    result = prereq_module._default_run_command(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout_seconds=0.01,
    )

    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_default_run_command_timeout_terminates_descendant_process(tmp_path):
    ticks_path = tmp_path / "ticks.txt"
    pid_path = tmp_path / "child.pid"
    child_code = (
        "import pathlib, sys, time\n"
        "ticks = pathlib.Path(sys.argv[1])\n"
        "pathlib.Path(sys.argv[2]).write_text(str(__import__('os').getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    with ticks.open('a', encoding='utf-8') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.05)\n"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        "ticks = pathlib.Path(sys.argv[1])\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[1], sys.argv[2]])\n"
        "deadline = time.time() + 3\n"
        "while not ticks.exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(10)\n"
    )

    child_pid = None
    try:
        result = prereq_module._default_run_command(
            [sys.executable, "-c", parent_code, str(ticks_path), str(pid_path), child_code],
            timeout_seconds=5,
        )
        assert result.returncode == 124

        size_after_timeout = ticks_path.stat().st_size
        time.sleep(0.3)
        assert ticks_path.stat().st_size == size_after_timeout
    finally:
        if pid_path.exists():
            child_pid = int(pid_path.read_text(encoding="utf-8"))
        if child_pid is not None:
            _terminate_test_process(child_pid)


def _terminate_test_process(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def test_prepare_direct_binary_installer_downloads_without_brew_or_go_and_runs_policy_update(tmp_path):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    payload = b"#!/bin/sh\necho infraguard\n"
    asset.write_bytes(payload)
    install_dir = tmp_path / "bin"
    progress_events: list[PrerequisiteProgress] = []
    post_install_paths: list[str] = []

    def command_exists(_command):
        return None

    def run_command(command, env=None):
        if command == ["infraguard", "policy", "update"]:
            post_install_paths.append((env or {}).get("PATH", ""))
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _direct_binary_prereqs(asset.as_uri(), str(install_dir), hashlib.sha256(payload).hexdigest()),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "direct-binary",
        progress_handler=progress_events.append,
    )

    installed = install_dir / "infraguard"
    assert installed.read_bytes() == payload
    assert os.access(installed, os.X_OK)
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(installed)
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(install_dir)
    assert post_install_paths == [resolution.env_overrides["PATH"]]
    assert any(event.phase == "download" and event.status == "output" for event in progress_events)
    assert any(event.phase == "post_install" and event.status == "succeeded" for event in progress_events)


def test_prepare_finds_existing_direct_binary_install_dir_before_prompting(tmp_path):
    install_dir = tmp_path / "bin"
    install_dir.mkdir()
    installed = install_dir / "infraguard"
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    installed.chmod(0o755)

    def choose_installer(_name, _installers):
        raise AssertionError("already installed infraguard should be found before prompting")

    resolution = prepare_prerequisites(
        _direct_binary_prereqs("https://example.com/infraguard", str(install_dir), "0" * 64),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=choose_installer,
    )

    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(installed)
    assert resolution.decisions["infraguard"].installer_id == "direct-binary"
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(install_dir)


def test_prepare_direct_binary_installer_reports_download_percent_when_content_length_is_known(tmp_path):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    payload = b"x" * 1536
    asset.write_bytes(payload)
    install_dir = tmp_path / "bin"
    progress_events: list[PrerequisiteProgress] = []

    resolution = prepare_prerequisites(
        _direct_binary_prereqs(asset.as_uri(), str(install_dir), hashlib.sha256(payload).hexdigest()),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
        progress_handler=progress_events.append,
    )

    download_events = [event for event in progress_events if event.phase == "download" and event.status == "output"]
    assert resolution.decisions["infraguard"].status == "available"
    assert download_events[-1].message == "Downloading infraguard-v0.10.0-darwin-arm64: 100% (1.5 KB / 1.5 KB)"
    assert download_events[-1].downloaded_bytes == len(payload)
    assert download_events[-1].total_bytes == len(payload)


def test_download_progress_reports_downloaded_size_when_total_is_unknown():
    progress_events: list[PrerequisiteProgress] = []

    prereq_module._emit_download_progress(
        progress_events.append,
        name="infraguard",
        installer_id="direct-binary",
        command=["download", "infraguard"],
        filename="infraguard",
        downloaded=1536,
        total=0,
    )

    assert progress_events[0].message == "Downloading infraguard: 1.5 KB downloaded"
    assert progress_events[0].downloaded_bytes == 1536
    assert progress_events[0].total_bytes is None


def test_prepare_direct_binary_installer_cleans_partial_download_on_keyboard_interrupt(tmp_path, monkeypatch):
    install_dir = tmp_path / "bin"
    prereqs = _direct_binary_prereqs(
        "https://example.com/infraguard-v0.10.0-darwin-arm64",
        str(install_dir),
        "0" * 64,
    )

    class InterruptingResponse:
        headers = {"Content-Length": "4096"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, _size):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        prereq_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: InterruptingResponse(),
    )

    with pytest.raises(KeyboardInterrupt):
        prepare_prerequisites(
            prereqs,
            feature_flags={"enable_reviewing": True},
            surface="repl",
            platform_system="darwin",
            platform_machine="arm64",
            command_exists=lambda _command: None,
            run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
            choose_installer=lambda _name, _installers: "direct-binary",
        )

    assert not (install_dir / ".infraguard.download").exists()


def test_prepare_direct_binary_installer_does_not_apply_download_timeout_by_default(tmp_path, monkeypatch):
    install_dir = tmp_path / "bin"
    payload = b"binary"
    prereqs = _direct_binary_prereqs(
        "https://example.com/infraguard-v0.10.0-darwin-arm64",
        str(install_dir),
        hashlib.sha256(payload).hexdigest(),
    )
    observed_timeouts = []

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, _size):
            if self._sent:
                return b""
            self._sent = True
            return payload

    def fake_urlopen(_url, *, timeout=None):
        observed_timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(prereq_module.urllib.request, "urlopen", fake_urlopen)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert observed_timeouts == [None]
    assert resolution.decisions["infraguard"].status == "available"


def test_prepare_direct_binary_installer_uses_configured_download_timeout(tmp_path, monkeypatch):
    install_dir = tmp_path / "bin"
    payload = b"binary"
    prereqs = _direct_binary_prereqs(
        "https://example.com/infraguard-v0.10.0-darwin-arm64",
        str(install_dir),
        hashlib.sha256(payload).hexdigest(),
    )
    prereqs["infraguard"]["installers"][0]["download"]["timeout_seconds"] = 120
    observed_timeouts = []

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, _size):
            if self._sent:
                return b""
            self._sent = True
            return payload

    def fake_urlopen(_url, *, timeout=None):
        observed_timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(prereq_module.urllib.request, "urlopen", fake_urlopen)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert observed_timeouts == [120.0]
    assert resolution.decisions["infraguard"].status == "available"


def test_prepare_direct_binary_installer_reports_incomplete_download_before_sha_mismatch(tmp_path, monkeypatch):
    install_dir = tmp_path / "bin"
    payload = b"partial"
    prereqs = _direct_binary_prereqs(
        "https://example.com/infraguard-v0.10.0-darwin-arm64",
        str(install_dir),
        "0" * 64,
    )

    class Response:
        headers = {"Content-Length": "100"}

        def __init__(self):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, _size):
            if self._sent:
                return b""
            self._sent = True
            return payload

    monkeypatch.setattr(
        prereq_module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: Response(),
    )

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    message = resolution.decisions["infraguard"].message
    assert "incomplete download for infraguard-v0.10.0-darwin-arm64" in message
    assert "expected 100 B, got 7 B" in message
    assert "sha256 mismatch" not in message


def test_prepare_direct_binary_installer_can_read_download_url_from_environment(tmp_path, monkeypatch):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    payload = b"binary"
    asset.write_bytes(payload)
    install_dir = tmp_path / "bin"
    prereqs = _direct_binary_prereqs("https://invalid.example/infraguard", str(install_dir), "")
    prereqs["infraguard"]["installers"][0]["download"]["assets"][0]["urls"] = [
        {"env": "IAC_CODE_TEST_INFRAGUARD_URL"},
        "https://invalid.example/infraguard",
    ]
    prereqs["infraguard"]["installers"][0]["download"]["assets"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setenv("IAC_CODE_TEST_INFRAGUARD_URL", asset.as_uri())

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert (install_dir / "infraguard").read_bytes() == payload
    assert resolution.decisions["infraguard"].status == "available"


def test_prepare_hides_direct_binary_installer_when_no_asset_matches_architecture(tmp_path):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    asset.write_bytes(b"binary")
    offered_installer_ids = []

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return None

    resolution = prepare_prerequisites(
        _direct_binary_prereqs(asset.as_uri(), str(tmp_path / "bin"), ""),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="amd64",
        command_exists=lambda _command: None,
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == []
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_prepare_existing_infraguard_on_path_does_not_run_installer_or_post_install():
    def unexpected_run_command(command, env=None):
        raise AssertionError(f"unexpected command: {command}")

    def unexpected_choose_installer(_name, _installers):
        raise AssertionError("installer should not be chosen")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=lambda command: f"/usr/bin/{command}" if command == "infraguard" else None,
        run_command=unexpected_run_command,
        choose_installer=unexpected_choose_installer,
    )

    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == "/usr/bin/infraguard"


def test_prepare_install_succeeds_but_post_install_fails_disables_review_for_run():
    available_commands = {"brew": "/usr/local/bin/brew"}

    def command_exists(command):
        return available_commands.get(command)

    def run_command(command, env=None):
        if command == ["brew", "install", "infraguard"]:
            available_commands["infraguard"] = "/usr/local/bin/infraguard"
        if command == ["infraguard", "policy", "update"]:
            return CommandResult(command=command, returncode=1, stdout="", stderr="policy failed")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "homebrew",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "post_install_failed"


def test_prepare_installer_commands_receive_configured_environment_without_persisting_to_post_install():
    prereqs = _infraguard_prereqs()
    prereqs["infraguard"]["installers"][0]["env"] = {
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_ENV_HINTS": "1",
    }
    available_commands = {"brew": "/usr/local/bin/brew"}
    brew_envs = []
    post_install_envs = []

    def command_exists(command):
        return available_commands.get(command)

    def run_command(command, env=None):
        if command[:1] == ["brew"]:
            brew_envs.append(dict(env or {}))
        if command == ["brew", "install", "infraguard"]:
            available_commands["infraguard"] = "/usr/local/bin/infraguard"
        if command == ["infraguard", "policy", "update"]:
            post_install_envs.append(dict(env or {}))
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "homebrew",
    )

    assert resolution.decisions["infraguard"].status == "available"
    assert brew_envs
    assert all(env["HOMEBREW_NO_AUTO_UPDATE"] == "1" for env in brew_envs)
    assert all(env["HOMEBREW_NO_ENV_HINTS"] == "1" for env in brew_envs)
    assert post_install_envs == [{}]


def test_prepare_install_failure_message_is_truncated_to_actionable_tail():
    available_commands = {"brew": "/usr/local/bin/brew"}
    noisy_stderr = "\n".join(
        [
            "==> Auto-updating Homebrew...",
            *(f"formula update line {index}" for index in range(200)),
            "fatal: early EOF",
            "fatal: fetch-pack: invalid index-pack output",
        ]
    )

    def run_command(command, env=None):
        return CommandResult(command=command, returncode=128, stdout="", stderr=noisy_stderr)

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=lambda command: available_commands.get(command),
        run_command=run_command,
        choose_installer=lambda _name, _installers: "homebrew",
    )

    message = resolution.decisions["infraguard"].message
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "fatal: early EOF" in message
    assert "invalid index-pack output" in message
    assert "formula update line 0" not in message
    assert len(message) < 1600


def test_prepare_run_command_exception_disables_review_instead_of_escaping():
    available_commands = {"brew": "/usr/local/bin/brew"}

    def run_command(command, env=None):
        raise OSError("exec format error")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        command_exists=lambda command: available_commands.get(command),
        run_command=run_command,
        choose_installer=lambda _name, _installers: "homebrew",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "exec format error" in resolution.decisions["infraguard"].message


def test_prepare_hides_direct_binary_installer_when_sha256_is_missing(tmp_path):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    asset.write_bytes(b"binary")
    install_dir = tmp_path / "bin"
    offered_installer_ids = []

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return "direct-binary"

    resolution = prepare_prerequisites(
        _direct_binary_prereqs(asset.as_uri(), str(install_dir), ""),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == []
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_prepare_direct_binary_installer_reports_invalid_url_as_install_failure(tmp_path):
    secret_url = "not a valid url?token=secret"
    prereqs = _direct_binary_prereqs(secret_url, str(tmp_path / "bin"), "0" * 64)
    progress_events: list[PrerequisiteProgress] = []

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
        progress_handler=progress_events.append,
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "download failed: ValueError" in resolution.decisions["infraguard"].message
    assert "secret" not in resolution.decisions["infraguard"].message
    assert all("secret" not in " ".join(event.command) for event in progress_events)


def test_prepare_direct_binary_installer_reports_invalid_http_url_without_leaking_token(tmp_path):
    secret_url = "http://example.com:bad?token=secret"
    prereqs = _direct_binary_prereqs(secret_url, str(tmp_path / "bin"), "0" * 64)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "secret" not in resolution.decisions["infraguard"].message
    assert "download failed: InvalidURL" in resolution.decisions["infraguard"].message


def test_prepare_direct_binary_installer_redacts_path_query_url_fragments(tmp_path):
    secret_url = "http://example.com/pa th?token=secret"
    prereqs = _direct_binary_prereqs(secret_url, str(tmp_path / "bin"), "0" * 64)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "token=secret" not in resolution.decisions["infraguard"].message
    assert "pa th" not in resolution.decisions["infraguard"].message


def test_prepare_direct_binary_installer_redacts_url_userinfo(tmp_path):
    secret_url = "http://user:pass@example.com/infraguard"
    prereqs = _direct_binary_prereqs(secret_url, str(tmp_path / "bin"), "0" * 64)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "user" not in resolution.decisions["infraguard"].message
    assert "pass" not in resolution.decisions["infraguard"].message


def test_prepare_direct_binary_installer_ignores_cleanup_failure_after_download_error(tmp_path, monkeypatch):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    asset.write_bytes(b"binary")
    prereqs = _direct_binary_prereqs(asset.as_uri(), str(tmp_path / "bin"), "0" * 64)

    original_unlink = Path.unlink

    def fail_unlink(self, *args, **kwargs):
        if self.name.startswith(".infraguard.download"):
            raise OSError("cleanup failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("iac_code.pipeline.engine.prerequisites.Path.unlink", fail_unlink)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "sha256 mismatch" in resolution.decisions["infraguard"].message


def test_prepare_direct_binary_installer_reports_install_dir_creation_failure(tmp_path, monkeypatch):
    prereqs = _direct_binary_prereqs((tmp_path / "missing").as_uri(), str(tmp_path / "bin"), "0" * 64)

    def fail_mkdir(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("iac_code.pipeline.engine.prerequisites.Path.mkdir", fail_mkdir)

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        run_command=lambda command, env=None: CommandResult(command=command, returncode=0, stdout="", stderr=""),
        choose_installer=lambda _name, _installers: "direct-binary",
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "install_failed"
    assert "permission denied" in resolution.decisions["infraguard"].message


def test_prepare_hides_direct_binary_installer_when_sha256_env_is_unset(tmp_path):
    asset = tmp_path / "infraguard-v0.10.0-darwin-arm64"
    asset.write_bytes(b"binary")
    prereqs = _direct_binary_prereqs(asset.as_uri(), str(tmp_path / "bin"), "")
    prereqs["infraguard"]["installers"][0]["download"]["assets"][0]["sha256"] = {
        "env": "IAC_CODE_TEST_INFRAGUARD_SHA256"
    }
    offered_installer_ids = []

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return None

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="darwin",
        platform_machine="arm64",
        command_exists=lambda _command: None,
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == []
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_prepare_go_install_path_hints_resolve_executable_and_use_path_for_post_install(tmp_path):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    install_envs = []
    post_install_env_paths = []

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        if command == [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]:
            install_envs.append(dict(env or {}))
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        if command == ["infraguard", "policy", "update"]:
            post_install_env_paths.append((env or {}).get("PATH", ""))
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
    )

    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(executable)
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(gopath_bin)
    assert install_envs
    assert install_envs[0]["GOPROXY"] == "https://mirrors.aliyun.com/goproxy/,direct"
    assert post_install_env_paths == [resolution.env_overrides["PATH"]]


def test_prepare_go_install_resolves_latest_cli_release_commit_from_github_tags(tmp_path, monkeypatch):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    commands_run = []

    class Response:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return self._payload

    def fake_urlopen(url, *, timeout=None):
        url = str(url)
        if url.endswith("/git/matching-refs/tags/v"):
            return Response(
                [
                    {"ref": "refs/tags/v0.9.0", "object": {"type": "tag", "sha": "tag-sha-090"}},
                    {"ref": "refs/tags/v0.10.1", "object": {"type": "tag", "sha": "tag-sha-101"}},
                ]
            )
        if url.endswith("/git/tags/tag-sha-101"):
            return Response({"object": {"type": "commit", "sha": "commit-sha-101"}})
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(prereq_module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(prereq_module.urllib.request, "urlopen", fake_urlopen)

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        commands_run.append(command)
        if command == [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@commit-sha-101",
        ]:
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        if command == ["infraguard", "policy", "update"]:
            return CommandResult(command=command, returncode=0, stdout="", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs_with_latest_go_install_target(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
    )

    assert [
        "go",
        "install",
        "github.com/aliyun/infraguard/cmd/infraguard@commit-sha-101",
    ] in commands_run
    assert resolution.decisions["infraguard"].status == "available"


def test_latest_go_install_target_prefers_git_ls_remote_over_github_api(monkeypatch):
    def fake_run(command, capture_output, text, check, timeout):
        assert command == ["git", "ls-remote", "--tags", "https://github.com/aliyun/infraguard.git", "refs/tags/v*"]

        return prereq_module.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "tag-sha-090\trefs/tags/v0.9.0\n"
                "commit-sha-090\trefs/tags/v0.9.0^{}\n"
                "tag-sha-101\trefs/tags/v0.10.1\n"
                "commit-sha-101\trefs/tags/v0.10.1^{}\n"
            ),
            stderr="",
        )

    def unexpected_urlopen(_url, *, timeout=None):
        raise AssertionError("GitHub API should not be called when git ls-remote succeeds")

    monkeypatch.setattr(prereq_module.subprocess, "run", fake_run)
    monkeypatch.setattr(prereq_module.urllib.request, "urlopen", unexpected_urlopen)

    target = prereq_module._resolve_latest_github_tag_go_install_target(
        {
            "kind": "go-install-latest-github-tag",
            "repo": "aliyun/infraguard",
            "module": "github.com/aliyun/infraguard/cmd/infraguard",
            "tag_prefix": "v",
            "fallback_ref": "fallback",
        }
    )

    assert target == "github.com/aliyun/infraguard/cmd/infraguard@commit-sha-101"


def test_prepare_go_install_uses_stable_fallback_when_latest_cli_release_detection_fails(tmp_path, monkeypatch):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    commands_run = []

    def fake_urlopen(_url, *, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(prereq_module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(prereq_module.urllib.request, "urlopen", fake_urlopen)

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        commands_run.append(command)
        if command == [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]:
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs_with_latest_go_install_target(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
    )

    assert [
        "go",
        "install",
        "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
    ] in commands_run
    assert resolution.decisions["infraguard"].status == "available"


def test_prepare_propagates_installer_display_metadata_to_progress_events(tmp_path):
    prereqs = _infraguard_prereqs()
    go_installer = next(
        installer for installer in prereqs["infraguard"]["installers"] if installer["id"] == "go-install"
    )
    go_installer["display_key"] = "go_install"
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    progress_events: list[PrerequisiteProgress] = []

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        if command[:2] == ["go", "install"]:
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        prereqs,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
        progress_handler=progress_events.append,
    )

    assert resolution.decisions["infraguard"].status == "available"
    assert any(event.installer_display_key == "go_install" for event in progress_events)


def test_prepare_finds_existing_go_install_path_hint_before_prompting(tmp_path):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    commands_run = []

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        commands_run.append(command)
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def choose_installer(_name, _installers):
        raise AssertionError("already installed infraguard should be found before prompting")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=choose_installer,
    )

    assert commands_run == [["go", "env", "GOBIN"], ["go", "env", "GOPATH"]]
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(executable)
    assert resolution.decisions["infraguard"].installer_id == "go-install"
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(gopath_bin)


def test_prepare_passes_default_timeout_to_path_hint_commands(tmp_path):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    observed_timeouts: list[tuple[list[str], float | None]] = []

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None, timeout_seconds=None):
        observed_timeouts.append((command, timeout_seconds))
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: None,
    )

    assert resolution.decisions["infraguard"].status == "available"
    assert observed_timeouts == [
        (["go", "env", "GOBIN"], 30.0),
        (["go", "env", "GOPATH"], 30.0),
    ]


def test_prepare_rejects_outdated_existing_go_install_path_hint(tmp_path):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    offered_installer_ids = []

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        if command == [str(executable), "version"]:
            return CommandResult(command=command, returncode=0, stdout="InfraGuard: 0.6.0\n", stderr="")
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return None

    resolution = prepare_prerequisites(
        _infraguard_prereqs_with_version_check(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == ["go-install"]
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"
    assert "0.6.0" in resolution.decisions["infraguard"].message


def test_prepare_go_install_prefers_gobin_path_hint_for_post_install(tmp_path):
    gobin = tmp_path / "custom-bin"
    gobin.mkdir()
    executable = gobin / "infraguard"
    commands_run = []
    post_install_env_paths = []

    def command_exists(command):
        return "/usr/local/bin/go" if command == "go" else None

    def run_command(command, env=None):
        commands_run.append(command)
        if command == [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]:
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOBIN"]:
            return CommandResult(command=command, returncode=0, stdout=str(gobin), stderr="")
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path / "gopath"), stderr="")
        if command == ["infraguard", "policy", "update"]:
            post_install_env_paths.append((env or {}).get("PATH", ""))
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
    )

    install_index = commands_run.index(
        [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]
    )
    assert ["go", "env", "GOPATH"] not in commands_run[install_index + 1 :]
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(executable)
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(gobin)
    assert post_install_env_paths == [resolution.env_overrides["PATH"]]


def test_prepare_windows_go_install_path_hints_resolve_exe_and_use_path_for_post_install(tmp_path):
    gopath_bin = tmp_path / "bin"
    gopath_bin.mkdir()
    executable = gopath_bin / "infraguard.exe"
    post_install_env_paths = []

    def command_exists(command):
        return "C:/Go/bin/go.exe" if command == "go" else None

    def run_command(command, env=None):
        if command == [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]:
            executable.write_text("@echo off\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path), stderr="")
        if command == ["infraguard", "policy", "update"]:
            post_install_env_paths.append((env or {}).get("PATH", ""))
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="windows",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
    )

    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(executable)
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(gopath_bin)
    assert post_install_env_paths == [resolution.env_overrides["PATH"]]


def test_prepare_windows_go_install_prefers_gobin_path_hint_for_post_install(tmp_path):
    gobin = tmp_path / "custom-bin"
    gobin.mkdir()
    executable = gobin / "infraguard.exe"
    commands_run = []
    post_install_env_paths = []

    def command_exists(command):
        return "C:/Go/bin/go.exe" if command == "go" else None

    def run_command(command, env=None):
        commands_run.append(command)
        if command == [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]:
            executable.write_text("@echo off\n", encoding="utf-8")
            executable.chmod(0o755)
        if command == ["go", "env", "GOBIN"]:
            return CommandResult(command=command, returncode=0, stdout=str(gobin), stderr="")
        if command == ["go", "env", "GOPATH"]:
            return CommandResult(command=command, returncode=0, stdout=str(tmp_path / "gopath"), stderr="")
        if command == ["infraguard", "policy", "update"]:
            post_install_env_paths.append((env or {}).get("PATH", ""))
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="windows",
        command_exists=command_exists,
        run_command=run_command,
        choose_installer=lambda _name, _installers: "go-install",
    )

    install_index = commands_run.index(
        [
            "go",
            "install",
            "github.com/aliyun/infraguard/cmd/infraguard@0a0f3e2427d883c4d5cbb6f1a4b7ebd78a029b43",
        ]
    )
    assert ["go", "env", "GOPATH"] not in commands_run[install_index + 1 :]
    assert resolution.feature_flags == {"enable_reviewing": True}
    assert resolution.decisions["infraguard"].status == "available"
    assert resolution.decisions["infraguard"].resolved_path == str(executable)
    assert resolution.env_overrides["PATH"].split(os.pathsep)[0] == str(gobin)
    assert post_install_env_paths == [resolution.env_overrides["PATH"]]


def test_prepare_windows_without_infraguard_and_without_go_offers_no_installer_and_disables_review():
    offered_installer_ids = ["not-called"]

    def choose_installer(_name, installers):
        offered_installer_ids[:] = [installer.id for installer in installers]
        return None

    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="windows",
        command_exists=lambda _command: None,
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == []
    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_selling_pipeline_direct_binary_installer_is_configured_for_declared_platforms(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IAC_CODE_INFRAGUARD_"):
            monkeypatch.delenv(key, raising=False)

    raw = yaml.safe_load(Path("src/iac_code/pipeline/selling/pipeline.yaml").read_text(encoding="utf-8"))
    prereqs = {"infraguard": raw["prerequisites"]["infraguard"]}
    cases = [
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("windows", "x86_64"),
        ("windows", "aarch64"),
    ]

    for platform_system, platform_machine in cases:
        offered_installer_ids = []

        def choose_installer(_name, installers):
            offered_installer_ids.extend(installer.id for installer in installers)
            return None

        prepare_prerequisites(
            prereqs,
            feature_flags={"enable_reviewing": True},
            surface="repl",
            platform_system=platform_system,
            platform_machine=platform_machine,
            command_exists=lambda _command: None,
            choose_installer=choose_installer,
        )

        assert offered_installer_ids == ["direct-binary"]


def test_prepare_declined_installer_disables_review():
    resolution = prepare_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        command_exists=lambda command: command == "go",
        choose_installer=lambda _name, _installers: None,
    )

    assert resolution.feature_flags == {"enable_reviewing": False}
    assert resolution.decisions["infraguard"].status == "declined_or_unavailable"


def test_to_metadata_returns_plain_dicts_not_dataclass_instances():
    resolution = inspect_prerequisites(
        _infraguard_prereqs(),
        feature_flags={"enable_reviewing": True},
        command_exists=lambda command: command,
    )

    metadata = resolution.to_metadata()

    assert isinstance(metadata, dict)
    assert not is_dataclass(metadata)
    assert isinstance(metadata["decisions"]["infraguard"], dict)
    assert not is_dataclass(metadata["decisions"]["infraguard"])
