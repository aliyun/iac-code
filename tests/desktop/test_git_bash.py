from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from iac_code.desktop import git_bash
from iac_code.utils.platform import _NPMMIRROR_CMD, GitBashNotFoundError


def test_inspect_git_bash_is_not_required_outside_windows(monkeypatch) -> None:
    monkeypatch.setattr(git_bash.sys, "platform", "linux")

    assert git_bash.inspect_git_bash() == {"status": "not_required", "path": None}


def test_inspect_git_bash_reports_missing_windows_shell(monkeypatch) -> None:
    monkeypatch.setattr(git_bash.sys, "platform", "win32")
    monkeypatch.setattr(
        git_bash,
        "_find_git_bash_path",
        MagicMock(side_effect=GitBashNotFoundError("missing")),
    )

    assert git_bash.inspect_git_bash() == {"status": "unavailable", "path": None}


def test_desktop_install_uses_external_process_adapter_and_redetects(monkeypatch) -> None:
    installed = r"C:\Program Files\Git\bin\bash.exe"
    find = MagicMock(side_effect=[GitBashNotFoundError("missing"), installed])
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    clear = MagicMock()
    monkeypatch.setattr(git_bash.sys, "platform", "win32")
    monkeypatch.setattr(git_bash, "_find_git_bash_path", find)
    monkeypatch.setattr(git_bash, "_clear_cache", clear)
    monkeypatch.setattr("iac_code.desktop.external_env.run_external", run)
    monkeypatch.setattr("iac_code.desktop.external_env.spawn_env_kwargs", lambda: {"env": {"PATH": "safe"}})

    result = git_bash.install_git_bash_for_desktop()

    assert result == {"status": "available", "path": installed}
    command = run.call_args.args[0]
    assert command == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _NPMMIRROR_CMD]
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["env"] == {"PATH": "safe"}
    clear.assert_called_once_with()
