"""Read-only physical-pause admission for this runtime's permission boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from iac_code.a2a.execution_owner import ExecutionOwnerIdentity
from iac_code.a2a.input_handoff import RecoverableInputHandoffProof
from iac_code.config import _resolve_config_dir
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock

logger = logging.getLogger(__name__)


class PendingPermissionPauseReferences:
    """An immutable index of boundaries published by this physical runtime."""

    ENABLED_ENV = "IAC_CODE_A2A_PERMISSION_PAUSE_ENABLED"

    def __init__(self, persistence_root: Path) -> None:
        # CSI claim can rebind this path after prewarming; its logical name,
        # not the current mount target, identifies the service's authority.
        self.persistence_root = Path(os.path.abspath(persistence_root))
        self.identity = ExecutionOwnerIdentity.capture()
        if not (
            self.identity.runtime_incarnation and self.identity.pid_namespace and self.identity.process_start_identity
        ):
            raise ValueError("permission_pause_runtime_identity_unavailable")
        self.manifest = {
            "schemaVersion": 1,
            "persistenceRoot": str(self.persistence_root),
            "runtimeIncarnation": self.identity.runtime_incarnation,
            "pidNamespace": self.identity.pid_namespace,
        }
        key = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        self.root = _resolve_config_dir() / "permission-pause" / key

    @classmethod
    def enabled(cls, persistence_root: Path | None) -> PendingPermissionPauseReferences | None:
        setting = os.environ.get(cls.ENABLED_ENV)
        if setting is not None:
            setting = setting.strip().lower()
            if setting in {"0", "false", "no", "off"}:
                return None
            if setting not in {"1", "true", "yes", "on"}:
                raise ValueError("permission_pause_invalid_enabled_setting")
        else:
            # These are the existing Linux sandbox image's runtime settings;
            # ordinary A2A/CLI/desktop sessions do not opt into physical drain.
            configured_root = os.environ.get("A2A_PERSISTENCE_DIR", "").strip()
            if (
                sys.platform != "linux"
                or persistence_root is None
                or not os.environ.get("IAC_CODE_CONFIG_BACKUP_TMP_DIR", "").strip()
                or not configured_root
                or os.path.abspath(configured_root) != os.path.abspath(persistence_root)
            ):
                return None
            identity = ExecutionOwnerIdentity.capture()
            if not (
                identity.runtime_incarnation
                and identity.runtime_incarnation.startswith("linux-boot:")
                and identity.pid_namespace
                and identity.process_start_identity
            ):
                return None
        if persistence_root is None:
            raise ValueError("permission_pause_persistence_unavailable")
        references = cls(persistence_root)
        references.initialize()
        return references

    def initialize(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with cross_process_file_lock(self.root / ".lock"):
            path = self.root / "manifest.json"
            if path.exists():
                self.validate_manifest()
            else:
                atomic_write_json(path, self.manifest, durable=True)

    def register(self, *, checkpoint_path: Path, cwd: str, record: dict[str, Any], control: dict[str, Any]) -> None:
        reference = {
            "schemaVersion": 1,
            "boundaryId": record["boundaryId"],
            "contextId": record["contextId"],
            "taskId": record["taskId"],
            "sessionId": record["sessionId"],
            "permissionClass": record["permissionClass"],
            "initialGeneration": record["generation"],
            "controlRevision": control["revision"],
            "executionId": control["executionId"],
            "owner": control["owner"],
            "ownerIdentity": control["ownerIdentity"],
            "checkpointPath": str(checkpoint_path.resolve()),
            "cwd": cwd,
        }
        self.validate_reference(reference)
        if any(control.get(key) != reference[key] for key in ("contextId", "taskId")):
            raise ValueError("permission_pause_control_mismatch")
        with cross_process_file_lock(self.root / ".lock"):
            self.validate_manifest()
            path = self.root / f"{reference['boundaryId']}.json"
            if path.exists():
                if self.read_document(path) != reference:
                    raise ValueError("permission_pause_reference_conflict")
            else:
                atomic_write_json(path, reference, durable=True)

    def validate_manifest(self) -> None:
        document = self.read_document(self.root / "manifest.json")
        if type(document.get("schemaVersion")) is not int or document != self.manifest:
            raise ValueError("permission_pause_manifest_mismatch")

    def retire_released(self, control: dict[str, Any] | None) -> None:
        """Drop ended references while the caller still fences the old control.

        A replacement control cannot prove the old cancellation drained. Reuse
        the exact old proof before its existing atomic claim overwrites it.
        Failure retains the reference without changing execution admission.
        """
        if control is None:
            return
        try:
            inspector = PendingPermissionPauseInspector(self.persistence_root)
            with ExitStack() as lock:
                try:
                    lock.enter_context(cross_process_file_lock(self.root / ".lock"))
                except RuntimeError as exc:
                    # The lock helper wraps acquire I/O failures; only entry is optional.
                    if isinstance(exc.__cause__, OSError):
                        raise exc.__cause__
                    raise
                for path in self.paths():
                    reference = self.read_document(path)
                    self.validate_reference(reference)
                    if path.name != f"{reference['boundaryId']}.json":
                        raise ValueError("permission_pause_reference_filename")
                    if any(
                        reference[key] != control.get(key)
                        for key in ("contextId", "taskId", "executionId", "ownerIdentity")
                    ):
                        continue
                    checkpoint = self.read_document(Path(reference["checkpointPath"]))
                    if checkpoint.get("phase") not in {"CANCELED", "RESOLVED"}:
                        continue
                    if inspector._classify_control(reference, {**checkpoint, "phase": "CANCELED"}, control) != "ready":
                        continue
                    if inspector._inspect_boundary(self, reference) in {"ready", "not_applicable"}:
                        path.unlink()
        except (OSError, ValueError, TypeError, KeyError):
            logger.warning("Permission pause reference retirement failed; retaining unverified references")

    def paths(self) -> list[Path]:
        self.validate_manifest()
        return sorted(path for path in self.root.iterdir() if path.suffix == ".json" and path.name != "manifest.json")

    def validate_reference(self, reference: dict[str, Any]) -> None:
        if type(reference.get("schemaVersion")) is not int or reference["schemaVersion"] != 1:
            raise ValueError("permission_pause_reference_version")
        for key in ("boundaryId", "contextId", "taskId", "sessionId", "executionId"):
            value = reference.get(key)
            if not isinstance(value, str) or not value or value in {".", ".."} or "/" in value or "\\" in value:
                raise ValueError("permission_pause_reference_identity")
        if reference.get("permissionClass") not in {"normal", "pipeline"}:
            raise ValueError("permission_pause_reference_class")
        for key, minimum in (("initialGeneration", 1), ("controlRevision", 0)):
            if type(reference.get(key)) is not int or reference[key] < minimum:
                raise ValueError("permission_pause_reference_generation")
        if not isinstance(reference.get("owner"), str) or not isinstance(reference.get("cwd"), str):
            raise ValueError("permission_pause_reference_owner")
        identity = ExecutionOwnerIdentity.model_validate(reference.get("ownerIdentity"))
        if not identity.process_start_identity or not self.identity.same_process_domain(identity):
            raise ValueError("permission_pause_reference_runtime")
        checkpoint = Path(reference["checkpointPath"])
        if (
            not checkpoint.is_absolute()
            or checkpoint.name != f"{reference['boundaryId']}.json"
            or checkpoint.parent.name != "permission-waits"
        ):
            raise ValueError("permission_pause_reference_path")

    @staticmethod
    def read_document(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise ValueError("permission_pause_symlink")
        document = json.loads(path.read_bytes())
        if not isinstance(document, dict):
            raise ValueError("permission_pause_invalid_document")
        return document


class PendingPermissionPauseInspector:
    """Inspect immutable references and existing proofs without advancing either."""

    def __init__(self, persistence_root: Path) -> None:
        self.persistence_root = persistence_root

    @staticmethod
    def result(status: str, reason: str, checked: int = 0) -> dict[str, Any]:
        return {"schemaVersion": 1, "status": status, "reason": reason, "checked": checked}

    def inspect(self) -> dict[str, Any]:
        checked = 0
        try:
            references = PendingPermissionPauseReferences(self.persistence_root)
            paths = references.paths()
            applicable = False
            pending = False
            for path in paths:
                reference = references.read_document(path)
                references.validate_reference(reference)
                if path.name != f"{reference['boundaryId']}.json":
                    raise ValueError("permission_pause_reference_filename")
                outcome = self._inspect_boundary(references, reference)
                checked += 1
                applicable |= outcome != "not_applicable"
                pending |= outcome == "pending"
            if paths != references.paths():
                return self.result("pending", "references_changed", checked)
            if pending:
                return self.result("pending", "handoff_not_ready", checked)
            return self.result("ready" if applicable else "not_applicable", "verified", checked)
        except (OSError, ValueError, TypeError, KeyError):
            return self.result("error", "inspection_failed", checked)

    def _inspect_boundary(self, references: PendingPermissionPauseReferences, reference: dict[str, Any]) -> str:
        checkpoint_path = Path(reference["checkpointPath"])
        record = references.read_document(checkpoint_path)
        if (
            type(record.get("schemaVersion")) is not int
            or record["schemaVersion"] != 1
            or any(record.get(key) != reference[key] for key in ("boundaryId", "contextId", "taskId", "sessionId"))
            or type(record.get("generation")) is not int
            or record["generation"] < reference["initialGeneration"]
        ):
            raise ValueError("permission_pause_checkpoint_identity")
        phase = record.get("phase")
        if phase not in {"WAITING", "TIMEOUT_GRACE", "SUSPENDING", "SUSPENDED", "RESTORING", "RESOLVED", "CANCELED"}:
            raise ValueError("permission_pause_checkpoint_phase")
        if phase not in {"RESOLVED", "CANCELED"} and record.get("permissionClass") != reference["permissionClass"]:
            raise ValueError("permission_pause_checkpoint_class")
        if phase == "RESOLVED":
            return "not_applicable"
        control_path = references.persistence_root / "execution-control" / f"{reference['contextId']}.json"
        control = references.read_document(control_path)
        outcome = self._classify_control(reference, record, control)
        if record != references.read_document(checkpoint_path) or control != references.read_document(control_path):
            return "pending"
        return outcome

    @staticmethod
    def _classify_control(reference: dict[str, Any], record: dict[str, Any], control: dict[str, Any]) -> str:
        if control.get("contextId") != reference["contextId"] or control.get("owner") != reference["owner"]:
            raise ValueError("permission_pause_control_identity")
        if control.get("executionId") != reference["executionId"] or control.get("taskId") != reference["taskId"]:
            # A replacement document alone does not prove the referenced
            # canceled runtime completed its original terminal release.
            return "pending"
        if control.get("ownerIdentity") != reference["ownerIdentity"]:
            raise ValueError("permission_pause_owner_changed")
        revision = control.get("revision")
        if (
            type(revision) is not int
            or revision <= reference["controlRevision"]
            or type(control.get("persistedRevision")) is not int
            or control.get("persistedRevision") != revision
            or control.get("commitError") is not None
        ):
            return "pending"
        from iac_code.a2a.execution_control import persisted_natural_handoff_admits_input_recovery

        if persisted_natural_handoff_admits_input_recovery(control, reference["contextId"]):
            return "ready"
        drained = (
            control.get("blockers") == []
            and control.get("streamAvailable") is False
            and type(control.get("subprocessToolTrackingVersion")) is int
            and control["subprocessToolTrackingVersion"] == 1
            and type(control.get("activeSubprocessTools")) is int
            and control["activeSubprocessTools"] == 0
        )
        if not drained:
            return "pending"
        if control.get("phase") == "terminated" and control.get("releaseReady") is True:
            backup = control.get("backup")
            return (
                "ready"
                if isinstance(backup, dict)
                and backup.get("status") in {"disabled", "shared_committed", "staged_committed"}
                else "pending"
            )
        if record["phase"] == "CANCELED" or control.get("phase") != "running":
            return "pending"
        if control.get("sessionId") != reference["sessionId"]:
            return "pending"
        handoff = control.get("inputHandoff")
        if handoff is not None:
            return (
                "ready"
                if (
                    RecoverableInputHandoffProof.validates_committed(
                        control, reference["contextId"], reference["taskId"]
                    )
                    and handoff.get("boundaryId") == reference["boundaryId"]
                    and handoff.get("boundaryGeneration") == record["generation"]
                )
                else "pending"
            )
        return (
            "ready"
            if (control.get("inputHandoffReady") is True and control.get("executionStatus") == "input-required")
            else "pending"
        )


class PermissionPauseCommand:
    """Private process-isolated probe; it never initializes reference storage."""

    def run(self) -> int:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--persistence-root", type=Path, required=True)
        arguments = parser.parse_args()
        result = PendingPermissionPauseInspector(arguments.persistence_root).inspect()
        print(json.dumps(result, sort_keys=True))
        return {"ready": 0, "not_applicable": 0, "pending": 1, "error": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(PermissionPauseCommand().run())
