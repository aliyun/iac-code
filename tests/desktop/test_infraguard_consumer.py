from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from iac_code.desktop.download_journal import install_lock_key
from iac_code.desktop.install_lock import DesktopInstallLease
from iac_code.pipeline.selling.tools import infraguard_scan_tool


@pytest.mark.asyncio
async def test_desktop_scan_holds_shared_install_lease_for_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_dir = tmp_path / "locks"
    managed_path = tmp_path / "bin" / "infraguard"
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setenv("IAC_CODE_DESKTOP_INSTALL_LOCK_DIR", str(lock_dir))
    monkeypatch.setenv("IAC_CODE_DESKTOP_INFRAGUARD_PATH", str(managed_path))
    key = install_lock_key(managed_path)
    writer = lock_dir / f"{key}.lock"

    async def fake_run(command, **_kwargs):
        assert command == [str(managed_path), "scan", "template.yaml"]
        with pytest.raises(TimeoutError):
            with DesktopInstallLease(writer, timeout=0):
                pass
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(infraguard_scan_tool, "_run_infraguard_command", fake_run)
    result, status = await infraguard_scan_tool._run_infraguard_with_desktop_lease(
        ["infraguard", "scan", "template.yaml"],
        cwd=str(tmp_path),
        timeout_seconds=1,
        env=None,
    )

    assert status is None
    assert result is not None and result.returncode == 0
    with DesktopInstallLease(writer, timeout=0):
        pass


@pytest.mark.asyncio
async def test_desktop_scan_refuses_incomplete_prerequisite_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_dir = tmp_path / "locks"
    managed_path = tmp_path / "bin" / "infraguard"
    lock_dir.mkdir()
    monkeypatch.setenv("IAC_CODE_DESKTOP_RUNTIME", "1")
    monkeypatch.setenv("IAC_CODE_DESKTOP_INSTALL_LOCK_DIR", str(lock_dir))
    monkeypatch.setenv("IAC_CODE_DESKTOP_INFRAGUARD_PATH", str(managed_path))
    key = install_lock_key(managed_path)
    (lock_dir / f"{key}.transaction.json").write_text("not-json", encoding="utf-8")

    result, status = await infraguard_scan_tool._run_infraguard_with_desktop_lease(
        ["infraguard", "scan", "template.yaml"],
        cwd=str(tmp_path),
        timeout_seconds=1,
        env=None,
    )

    assert result is None
    assert status == "recovery_required"
