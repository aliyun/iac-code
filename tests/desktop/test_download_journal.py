from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from iac_code.desktop import download_journal
from iac_code.desktop.download_journal import (
    DesktopDownloadTransaction,
    DesktopPrerequisiteConsumerLease,
    DesktopPrerequisiteRecipe,
    DesktopPrerequisiteRepairLease,
    DesktopRecoveryRequiredError,
    DesktopTransactionReader,
    current_infraguard_recovery_recipe,
    has_recovery_required,
    install_lock_key,
    recover_install_transactions,
)
from iac_code.desktop.install_lock import DesktopInstallLease
from iac_code.desktop.runtime import DesktopInstallContext


def _context(tmp_path: Path) -> DesktopInstallContext:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    locks = tmp_path / "locks"
    runtime.mkdir()
    state.mkdir()
    return DesktopInstallContext(
        install_id="iac-code-test",
        runtime_dir=runtime,
        host_state_dir=state,
        install_lock_dir=locks,
        sidecar_generation=7,
    )


def _recipe(final_path: Path, payload: bytes = b"infraguard") -> DesktopPrerequisiteRecipe:
    return DesktopPrerequisiteRecipe(
        prerequisite="infraguard",
        consumer_impact="prerequisite",
        installer_id="direct-binary",
        final_path=final_path.resolve(),
        platform_name="linux",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        minimum_version="0.10.1",
        version_command=("infraguard", "version"),
        version_pattern=r"InfraGuard:\s*(?P<version>\d+\.\d+\.\d+)",
        post_install_commands=(("infraguard", "policy", "update"),),
        post_install_timeout=30,
    )


def test_transaction_is_persisted_before_download_and_cancel_removes_it(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()

    with DesktopDownloadTransaction(context, final_path) as transaction:
        temporary = transaction.begin(
            final_path,
            installer_id="direct-binary",
            expected_sha256="a" * 64,
            platform_name="linux",
        )
        record = json.loads(transaction.journal_path.read_text(encoding="utf-8"))
        assert record["phase"] == "downloading"
        assert record["temporaryPath"] == str(temporary)
        temporary.write_bytes(b"partial")
        transaction.cancel_before_replace()

    assert not temporary.exists()
    assert not transaction.journal_path.exists()


def test_transaction_complete_removes_only_after_terminal_phase(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()

    with DesktopDownloadTransaction(context, final_path) as transaction:
        transaction.begin(
            final_path,
            installer_id="direct-binary",
            expected_sha256="b" * 64,
            platform_name="linux",
        )
        transaction.transition("replaced_pending_validation")
        assert transaction.journal_path.exists()
        transaction.complete()

    assert not transaction.journal_path.exists()


def test_startup_recovery_cleans_only_safe_pre_replace_record(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    transaction = DesktopDownloadTransaction(context, final_path)
    with transaction:
        temporary = transaction.begin(
            final_path,
            installer_id="direct-binary",
            expected_sha256="c" * 64,
            platform_name="linux",
        )
        temporary.write_bytes(b"partial")

    assert recover_install_transactions(context) == ()
    assert not temporary.exists()
    assert not transaction.journal_path.exists()


def test_startup_recovery_preserves_post_replace_record_as_degraded(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    transaction = DesktopDownloadTransaction(context, final_path)
    with transaction:
        transaction.begin(
            final_path,
            installer_id="direct-binary",
            expected_sha256="d" * 64,
            platform_name="linux",
        )
        transaction.transition("validated_pending_post_install")

    assert recover_install_transactions(context) == ("infraguard",)
    assert transaction.journal_path.exists()
    with DesktopDownloadTransaction(context, final_path) as retry:
        with pytest.raises(DesktopRecoveryRequiredError):
            retry.begin(
                final_path,
                installer_id="direct-binary",
                expected_sha256="d" * 64,
                platform_name="linux",
            )


def test_corrupt_journal_is_preserved_and_reported_as_managed_prerequisite(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.install_lock_dir.mkdir()
    journal = context.install_lock_dir / (("0" * 64) + ".transaction.json")
    journal.write_text("not-json", encoding="utf-8")

    assert recover_install_transactions(context) == ("infraguard",)
    assert journal.exists()


def test_reader_observes_transaction_under_shared_lease(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    with DesktopDownloadTransaction(context, final_path) as transaction:
        transaction.begin(
            final_path,
            installer_id="direct-binary",
            expected_sha256="e" * 64,
            platform_name="linux",
        )

    with DesktopTransactionReader(context, final_path) as reader:
        assert reader.recovery_required() is True


def test_startup_recovery_completes_fixed_replace_validation_and_post_install(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    payload = b"infraguard"
    recipe = _recipe(final_path, payload)
    transaction = DesktopDownloadTransaction(context, final_path, recipe=recipe)
    with transaction:
        temporary = transaction.begin(
            final_path,
            installer_id=recipe.installer_id,
            expected_sha256=recipe.expected_sha256,
            platform_name=recipe.platform_name,
        )
        temporary.write_bytes(payload)
        transaction.transition("replace_pending")

    commands: list[list[str]] = []

    def run(command, _timeout):
        commands.append(list(command))
        output = "InfraGuard: 0.10.1" if command[-1] == "version" else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    assert recover_install_transactions(context, recipes=(recipe,), run_command=run) == ()
    assert final_path.read_bytes() == payload
    assert commands == [
        [str(final_path), "version"],
        [str(final_path), "policy", "update"],
    ]
    assert not transaction.journal_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="simulates Windows FlushFileBuffers semantics on POSIX")
def test_replace_pending_recovery_does_not_fsync_read_only_handle_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows must flush replace-pending artifacts through a writable handle."""
    import fcntl

    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    payload = b"infraguard"
    recipe = _recipe(final_path, payload)
    transaction = DesktopDownloadTransaction(context, final_path, recipe=recipe)
    with transaction:
        temporary = transaction.begin(
            final_path,
            installer_id=recipe.installer_id,
            expected_sha256=recipe.expected_sha256,
            platform_name=recipe.platform_name,
        )
        temporary.write_bytes(payload)
        transaction.transition("replace_pending")

    real_os = download_journal.os

    class _WindowsLikeOS:
        """``os`` proxy scoped to ``download_journal`` reporting ``name == 'nt'``
        and rejecting flushes of read-only handles, exactly as Windows would.
        The real lock backend keeps using the unpatched POSIX ``os``."""

        name = "nt"

        def __getattr__(self, item):
            return getattr(real_os, item)

        @staticmethod
        def fsync(fd: int) -> None:
            if fcntl.fcntl(fd, fcntl.F_GETFL) & real_os.O_ACCMODE == real_os.O_RDONLY:
                raise PermissionError("FlushFileBuffers is not permitted on a read-only handle")
            real_os.fsync(fd)

    monkeypatch.setattr(download_journal, "os", _WindowsLikeOS())

    def run(command, _timeout):
        output = "InfraGuard: 0.10.1" if command[-1] == "version" else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    assert recover_install_transactions(context, recipes=(recipe,), run_command=run) == ()
    assert final_path.read_bytes() == payload
    assert not transaction.journal_path.exists()


def test_changed_recipe_is_degraded_without_running_journal_commands(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    original = _recipe(final_path)
    transaction = DesktopDownloadTransaction(context, final_path, recipe=original)
    with transaction:
        transaction.begin(
            final_path,
            installer_id=original.installer_id,
            expected_sha256=original.expected_sha256,
            platform_name=original.platform_name,
        )
        transaction.transition("validated_pending_post_install")
    changed = DesktopPrerequisiteRecipe(
        **{**original.__dict__, "minimum_version": "0.11.0"},
    )

    def fail_if_called(_command, _timeout):
        raise AssertionError("an unrecognized journal must never provide executable recovery commands")

    assert recover_install_transactions(context, recipes=(changed,), run_command=fail_if_called) == ("infraguard",)
    assert transaction.journal_path.exists()


def test_recovery_queries_are_precisely_scoped(tmp_path: Path) -> None:
    context = _context(tmp_path)
    infraguard = tmp_path / "bin" / "infraguard"
    other = tmp_path / "bin" / "other"
    infraguard.parent.mkdir()
    with DesktopDownloadTransaction(context, infraguard) as transaction:
        transaction.begin(
            infraguard,
            installer_id="direct-binary",
            expected_sha256="f" * 64,
            platform_name="linux",
        )

    assert has_recovery_required(context, prerequisite="infraguard") is True
    assert has_recovery_required(context, final_path=infraguard) is True
    assert has_recovery_required(context, final_path=other) is False
    assert has_recovery_required(context, prerequisite="terraform") is False


def test_consumer_shared_lease_blocks_writer_and_checks_prerequisite_scope(tmp_path: Path) -> None:
    context = _context(tmp_path)
    current = tmp_path / "bin" / "infraguard"
    legacy = tmp_path / "old-bin" / "infraguard"
    current.parent.mkdir()
    legacy.parent.mkdir()
    with DesktopDownloadTransaction(context, legacy) as transaction:
        transaction.begin(
            legacy,
            installer_id="direct-binary",
            expected_sha256="1" * 64,
            platform_name="linux",
        )

    with DesktopPrerequisiteConsumerLease(
        context.install_lock_dir,
        current,
        prerequisite="infraguard",
        timeout=0,
    ) as consumer:
        assert consumer.recovery_required() is True
        current_key = install_lock_key(current)
        with pytest.raises(TimeoutError):
            with DesktopInstallLease(context.install_lock_dir / f"{current_key}.lock", timeout=0):
                pass


def test_failed_force_repair_restores_the_previous_recovery_record(tmp_path: Path) -> None:
    context = _context(tmp_path)
    final_path = tmp_path / "bin" / "infraguard"
    final_path.parent.mkdir()
    recipe = _recipe(final_path)
    with DesktopDownloadTransaction(context, final_path, recipe=recipe) as interrupted:
        interrupted.begin(
            final_path,
            installer_id=recipe.installer_id,
            expected_sha256=recipe.expected_sha256,
            platform_name=recipe.platform_name,
        )
        interrupted.transition("validated_pending_post_install")
    previous = interrupted.journal_path.read_bytes()

    with DesktopDownloadTransaction(context, final_path, recipe=recipe, force_repair=True) as repair:
        temporary = repair.begin(
            final_path,
            installer_id=recipe.installer_id,
            expected_sha256=recipe.expected_sha256,
            platform_name=recipe.platform_name,
        )
        temporary.write_bytes(b"partial")
        # A fallback URL reuses the same repair transaction.  Beginning that
        # attempt must not replace the snapshot of the original recovery log
        # with the first attempt's transient ``downloading`` record.
        repair.begin(
            final_path,
            installer_id=recipe.installer_id,
            expected_sha256=recipe.expected_sha256,
            platform_name=recipe.platform_name,
        )
        repair.cancel_before_replace()

    assert interrupted.journal_path.read_bytes() == previous
    assert has_recovery_required(context, prerequisite="infraguard") is True


def test_repair_exclusively_holds_legacy_and_current_keys(tmp_path: Path) -> None:
    context = _context(tmp_path)
    current = tmp_path / "bin" / "infraguard"
    legacy = tmp_path / "legacy" / "infraguard"
    legacy.parent.mkdir()
    with DesktopDownloadTransaction(context, legacy) as transaction:
        transaction.begin(
            legacy,
            installer_id="direct-binary",
            expected_sha256="2" * 64,
            platform_name="linux",
        )

    with DesktopPrerequisiteRepairLease(context, current, timeout=0):
        with pytest.raises(TimeoutError):
            with DesktopPrerequisiteConsumerLease(
                context.install_lock_dir,
                current,
                prerequisite="infraguard",
                timeout=0,
            ):
                pass


@pytest.mark.asyncio
async def test_normal_desktop_install_cannot_bypass_recovery_required(tmp_path: Path) -> None:
    from iac_code.web.pipeline_prerequisites import stream_install_review_step_prerequisite

    context = _context(tmp_path)
    recipe = current_infraguard_recovery_recipe()
    with DesktopDownloadTransaction(context, recipe.final_path, recipe=recipe) as transaction:
        transaction.begin(
            recipe.final_path,
            installer_id=recipe.installer_id,
            expected_sha256=recipe.expected_sha256,
            platform_name=recipe.platform_name,
        )
        transaction.transition("validated_pending_post_install")

    events = [event async for event in stream_install_review_step_prerequisite(context)]

    assert events == [
        {
            "phase": "result",
            "status": "error",
            "satisfied": False,
            "prerequisite_status": "recovery_required",
            "message": "Desktop prerequisite repair is required",
        }
    ]
