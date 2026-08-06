from __future__ import annotations

import subprocess
import time

from iac_code.desktop import diagnostics


def test_tool_version_timeout_is_reported_as_timeout_not_unavailable(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "git"
    executable.write_text("desktop-test", encoding="utf-8")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: str(executable))
    monkeypatch.setattr(
        diagnostics,
        "run_external",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", kwargs["timeout"])),
    )

    result = diagnostics._tool_status("git")

    assert result == {"status": "timeout", "path": str(executable.resolve()), "version": None}


def test_tool_version_timeout_is_clamped_to_probe_deadline(monkeypatch, tmp_path) -> None:
    executable = tmp_path / "git"
    executable.write_text("desktop-test", encoding="utf-8")
    observed: list[float] = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: str(executable))

    def run(*args, **kwargs):
        observed.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, stdout="git version test\n", stderr="")

    monkeypatch.setattr(diagnostics, "run_external", run)
    deadline = time.monotonic() + 0.25

    result = diagnostics._tool_status("git", deadline)

    assert result["status"] == "available"
    assert observed and 0 < observed[0] <= 0.25
