"""Subcommand body for `iac-code install-git-bash` (Windows only).

The function is defined here, not in cli/main.py, so it can be imported
and tested directly on non-Windows CI runners. cli/main.py registers it
on the typer app only when sys.platform == "win32".
"""

from __future__ import annotations

import subprocess

import typer

from iac_code.i18n import _
from iac_code.utils.platform import (
    _NPMMIRROR_CMD,
    GitBashNotFoundError,
    _clear_cache,
    _find_git_bash_path,
)


def install_git_bash() -> None:
    """Install Git for Windows via the npmmirror mirror.

    Flow:
      1. If bash.exe is already discoverable, print the path and exit 0.
      2. Otherwise launch PowerShell with _NPMMIRROR_CMD; stdio is
         inherited so download progress, Inno Setup's progress dialog,
         and the UAC prompt are visible to the user.
      3. After PowerShell exits, re-detect bash.exe. Success -> exit 0;
         any failure path -> exit 1 with a translated diagnostic.
    """
    try:
        path: str | None = _find_git_bash_path()
    except GitBashNotFoundError:
        path = None

    if path:
        typer.echo("✓ " + _("Git Bash is already installed at {}").format(path))
        raise typer.Exit(0)

    typer.echo(_("Installing Git for Windows via npmmirror..."))

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _NPMMIRROR_CMD,
            ],
            check=False,
        )
    except FileNotFoundError:
        typer.echo(
            _("powershell.exe was not found on PATH; cannot run installer."),
            err=True,
        )
        raise typer.Exit(1)

    if result.returncode != 0:
        typer.echo(
            _("Installation failed (PowerShell exited with code {})").format(result.returncode),
            err=True,
        )
        raise typer.Exit(1)

    _clear_cache()
    try:
        path = _find_git_bash_path()
    except GitBashNotFoundError:
        typer.echo(
            _(
                "Installer exited but bash.exe was not found in common locations; "
                "UAC may have been cancelled or the installer used a non-standard path."
            ),
            err=True,
        )
        raise typer.Exit(1)

    typer.echo("✓ " + _("Git for Windows installed at {}").format(path))
    raise typer.Exit(0)
