from __future__ import annotations

import shlex

import typer

from iac_code import __version__
from iac_code.i18n import _
from iac_code.services.update_checker import check_for_updates_once, run_update_command


def update(
    check: bool = typer.Option(False, "--check", help=_("Check for updates without installing.")),
) -> None:
    """Update iac-code to the latest available version."""
    state = check_for_updates_once(current_version=__version__, force=True)
    pending = state.pending
    if pending is None:
        typer.echo(_("iac-code is already up to date (v{}).").format(__version__))
        raise typer.Exit()

    if check:
        typer.echo(_("Update available: v{} -> v{}").format(pending.current_version, pending.version))
        typer.echo(_("Run {} to update.").format(shlex.join(pending.update_command)))
        raise typer.Exit()

    typer.echo(_("Updating iac-code from v{} to v{}...").format(pending.current_version, pending.version))
    try:
        result = run_update_command(pending)
    except OSError as exc:
        typer.echo(_("Update command failed: {}").format(exc), err=True)
        raise typer.Exit(1) from exc

    if result.returncode != 0:
        typer.echo(_("Update command failed with exit code {}.").format(result.returncode), err=True)
        raise typer.Exit(result.returncode)

    typer.echo(_("Successfully updated to v{}!").format(pending.version))
