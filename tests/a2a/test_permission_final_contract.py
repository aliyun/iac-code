"""Cloud permission admission always requires the committed boundary proof."""

from types import SimpleNamespace

import pytest

from iac_code.a2a.permission_pause import PendingPermissionPauseReferences

from .test_permission_pause import PauseFixture


class FinalPermissionContract:
    def __init__(self, tmp_path, monkeypatch):
        self.pause = PauseFixture(tmp_path, monkeypatch)
        self.pause.identity = self.pause.identity.model_copy(update={"runtime_incarnation": "linux-boot:test"})
        monkeypatch.setattr("iac_code.a2a.permission_pause.sys", SimpleNamespace(platform="linux"))
        monkeypatch.setenv("A2A_PERSISTENCE_DIR", str(self.pause.persistence))
        monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "shared"))
        monkeypatch.delenv("IAC_CODE_CONFIG_BACKUP_TMP_DIR", raising=False)
        monkeypatch.delenv(PendingPermissionPauseReferences.ENABLED_ENV, raising=False)


def test_cloud_direct_backup_enables_permission_index(tmp_path, monkeypatch):
    contract = FinalPermissionContract(tmp_path, monkeypatch)

    assert PendingPermissionPauseReferences.enabled(contract.pause.persistence) is not None


def test_cloud_permission_protection_cannot_be_disabled(tmp_path, monkeypatch):
    contract = FinalPermissionContract(tmp_path, monkeypatch)
    monkeypatch.setenv(PendingPermissionPauseReferences.ENABLED_ENV, "false")

    with pytest.raises(ValueError, match="permission_pause_cloud_protection_required"):
        PendingPermissionPauseReferences.enabled(contract.pause.persistence)


def test_inspector_identifies_the_strict_protocol(tmp_path, monkeypatch):
    pause = PauseFixture(tmp_path, monkeypatch)
    pause.committed()

    assert pause.inspector.inspect() == {"schemaVersion": 2, "status": "ready", "reason": "verified", "checked": 1}


@pytest.mark.parametrize("permission_class", ["normal", "pipeline"])
def test_permission_index_rejects_generic_input_ready(tmp_path, monkeypatch, permission_class):
    pause = PauseFixture(tmp_path, monkeypatch)
    pause.record["permissionClass"] = permission_class
    pause.references.root.joinpath("pwb_boundary.json").unlink()
    pause.register()
    pause.control.update(
        revision=3,
        persistedRevision=3,
        executionStatus="input-required",
        streamAvailable=False,
        inputHandoffReady=True,
    )
    pause.write()

    assert pause.inspector.inspect()["status"] == "pending"


@pytest.mark.parametrize("backup", ["disabled", "staged_committed", "shared_committed"])
def test_unanswered_permission_cannot_use_terminal_release_as_recovery_proof(tmp_path, monkeypatch, backup):
    pause = PauseFixture(tmp_path, monkeypatch)
    pause.control.update(
        phase="terminated",
        revision=3,
        persistedRevision=3,
        releaseReady=True,
        streamAvailable=False,
        backup={"status": backup},
    )
    pause.write()

    assert pause.inspector.inspect()["status"] == "pending"
