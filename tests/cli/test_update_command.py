from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from iac_code.services.update_checker import OFFICIAL_PYPI_SOURCE, PendingUpdate, UpdateState

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pending_update() -> PendingUpdate:
    return PendingUpdate(
        version="0.6.0",
        current_version="0.5.0",
        source=OFFICIAL_PYPI_SOURCE,
        checked_at=1000.0,
        update_command=("/python", "-m", "pip", "install", "--upgrade", "iac-code"),
        release_notes_url="https://github.com/aliyun/iac-code/releases/tag/v0.6.0",
    )


def test_update_check_reports_available_update_without_installing():
    from iac_code.cli.main import app

    pending = _pending_update()
    with (
        patch("iac_code.cli.update.check_for_updates_once", return_value=UpdateState(pending=pending)) as check,
        patch("iac_code.cli.update.run_update_command") as run_update,
    ):
        result = runner.invoke(app, ["update", "--check"])

    assert result.exit_code == 0
    assert "Update available: v0.5.0 -> v0.6.0" in result.output
    assert "Run /python -m pip install --upgrade iac-code to update." in result.output
    check.assert_called_once()
    assert check.call_args.kwargs["force"] is True
    run_update.assert_not_called()


def test_update_check_help_is_translated_in_chinese_locale():
    env = os.environ.copy()
    env["LANGUAGE"] = "zh"
    env["LC_ALL"] = "zh_CN.UTF-8"
    env["LANG"] = "zh_CN.UTF-8"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from typer.testing import CliRunner\n"
                "from iac_code.cli.main import app\n"
                "result = CliRunner().invoke(app, ['update', '-h'])\n"
                "print(result.output)\n"
                "raise SystemExit(result.exit_code)\n"
            ),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "仅检查更新，不安装。" in result.stdout
    assert "Check for updates without installing." not in result.stdout


def test_update_runs_detected_update_command_and_prints_summary():
    from iac_code.cli.main import app

    pending = _pending_update()
    completed = subprocess.CompletedProcess(args=pending.update_command, returncode=0)
    with (
        patch("iac_code.cli.update.check_for_updates_once", return_value=UpdateState(pending=pending)),
        patch("iac_code.cli.update.run_update_command", return_value=completed) as run_update,
    ):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "Updating iac-code from v0.5.0 to v0.6.0..." in result.output
    assert "Successfully updated to v0.6.0!" in result.output
    run_update.assert_called_once_with(pending)


def test_update_reports_current_version_when_no_update_is_available():
    from iac_code.cli.main import app

    with patch("iac_code.cli.update.check_for_updates_once", return_value=UpdateState(pending=None)):
        result = runner.invoke(app, ["update", "--check"])

    assert result.exit_code == 0
    assert "iac-code is already up to date" in result.output


def test_update_exits_nonzero_when_update_command_fails():
    from iac_code.cli.main import app

    pending = _pending_update()
    completed = subprocess.CompletedProcess(args=pending.update_command, returncode=7)
    with (
        patch("iac_code.cli.update.check_for_updates_once", return_value=UpdateState(pending=pending)),
        patch("iac_code.cli.update.run_update_command", return_value=completed),
    ):
        result = runner.invoke(app, ["update"])

    assert result.exit_code == 7
    assert "Update command failed with exit code 7." in result.output
