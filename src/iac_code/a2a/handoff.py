"""Typed, cooperative A2A/AG-UI handoff over immutable shared snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from iac_code.a2a.guidance_input import GuidanceInputJournal
from iac_code.a2a.persistence import A2APersistenceStore, _protocol_id_file_stem
from iac_code.a2a.types import validate_protocol_id
from iac_code.agui.state import FileAguiThreadStateStore
from iac_code.config import get_config_dir
from iac_code.services.handoff_fence import HandoffFrozenError, SessionWriterFence
from iac_code.services.session_backup import SessionBackupService
from iac_code.services.session_layout import require_supported_session_layout
from iac_code.services.session_storage import SessionStorage
from iac_code.utils.state_io import atomic_write_json, cross_process_file_lock

HANDOFF_VERSION = "session-handoff-v1"


class HandoffDocument(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class PhysicalExecutionIdentity(HandoffDocument):
    sandbox_id: str = Field(min_length=1)
    activation_id: str | None = None
    run_id: str = Field(min_length=1)
    lease_id: str | None = None
    identity_kind: Literal["slot", "legacy"] = "slot"
    quota_lease_id: str | None = None

    @model_validator(mode="after")
    def validate_owner_identity(self):
        if self.identity_kind == "slot" and (not self.activation_id or not self.lease_id):
            raise ValueError("Slot identity requires activation and lease")
        if self.identity_kind == "legacy" and not self.quota_lease_id:
            raise ValueError("Legacy identity requires its physical quota lease")
        return self


class HandoffPrepareRequest(HandoffDocument):
    user_id: str = Field(min_length=1)
    backend_scope: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    internal_session_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    protocol: Literal["a2a", "agui"]
    mode: Literal["normal", "pipeline"]
    source: PhysicalExecutionIdentity
    migration_id: str = Field(min_length=1)
    epoch: int = Field(gt=0, strict=True)
    agui_identity: dict[str, Any] | None = None
    pending_input: dict[str, Any] | None = None
    input_digest: str | None = None
    guidance_id: str | None = None
    input_application_unknown: bool = False

    @model_validator(mode="after")
    def validate_protocol_identity(self):
        validate_protocol_id(self.context_id)
        validate_protocol_id(self.task_id)
        validate_protocol_id(self.internal_session_id)
        return self


class MigrationReceipt(HandoffPrepareRequest):
    version: Literal["session-handoff-v1"] = HANDOFF_VERSION
    commit_id: str = Field(min_length=1)
    manifest_digest: str = Field(pattern="^[0-9a-f]{64}$")
    backup_generation: int = Field(gt=0, strict=True)
    business_revision: int = Field(ge=0, strict=True)
    source_quiesced: Literal[True]
    shared_committed: Literal[True]
    recovery_kind: Literal["terminal", "input_required"]
    permission_generation: int | None = Field(default=None, ge=0, strict=True)
    source_checkpoint_commit_id: str | None = None
    input_acceptance: Literal["APPLIED", "NOT_APPLIED_CONFIRMED", "UNKNOWN"] = "UNKNOWN"

    @field_validator("source_quiesced", "shared_committed", mode="before")
    @classmethod
    def require_true_proof(cls, value: Any) -> Literal[True]:
        if value is not True:
            raise ValueError("Handoff proof must be an explicit true boolean")
        return True


class HandoffDiscoverRequest(HandoffDocument):
    request: HandoffPrepareRequest
    checkpoint: MigrationReceipt | None = None


class HandoffRestoreRequest(HandoffDocument):
    receipt: MigrationReceipt
    target: PhysicalExecutionIdentity


class HandoffAuthorizeRequest(HandoffRestoreRequest):
    commit_id: str = Field(min_length=1)


class HandoffInputRequest(HandoffRestoreRequest):
    input_digest: str = Field(min_length=1)
    pending_input: dict[str, Any]
    caller_metadata: dict[str, Any] | None = Field(default=None, repr=False)
    skip_delivery: bool = False


class HandoffInputResult(HandoffDocument):
    status: Literal["APPLIED", "ALREADY_APPLIED", "NOT_READY", "CONFLICT", "UNKNOWN"]
    reason: str | None = None
    input_digest: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    execution_id: str | None = None


class HandoffResult(HandoffDocument):
    status: Literal[
        "PREPARED", "RESTORED", "AUTHORIZED", "NOT_READY", "NOT_FOUND", "CONFLICT", "UNSUPPORTED", "UNKNOWN"
    ]
    receipt: MigrationReceipt | None = None
    reason: str | None = None
    target: PhysicalExecutionIdentity | None = None
    commit_id: str | None = None


class SessionHandoffService:
    def __init__(
        self,
        *,
        task_store: Any,
        controls: Any,
        persistence_root: Path | None,
        shared_root: Path | None = None,
        private_root: Path | None = None,
        storage: SessionStorage | None = None,
        agui_store: FileAguiThreadStateStore | None = None,
        input_delivery: Any = None,
    ) -> None:
        self.input_delivery = input_delivery
        self.task_store = task_store
        self.controls = controls
        self.persistence_root = persistence_root
        raw = os.environ.get("IAC_CODE_HANDOFF_SHARED_DIR", "")
        self.shared_root = shared_root or (Path(raw) if raw else None)
        self.private_root = private_root or get_config_dir() / "handoff-prepared"
        self.fence = SessionWriterFence()
        self.storage = storage or SessionStorage()
        self.agui_store = agui_store or FileAguiThreadStateStore()

    def capabilities(self) -> dict[str, Any]:
        supported = self.shared_root is not None and self.persistence_root is not None and self.fence.enabled
        if supported:
            shared_paths = [self.shared_root]
            if os.environ.get("IAC_CODE_CONFIG_BACKUP_DIR"):
                shared_paths.append(Path(os.environ["IAC_CODE_CONFIG_BACKUP_DIR"]))
            supported = not any(self.fence.root.resolve().is_relative_to(path.resolve()) for path in shared_paths)
        return {
            "version": HANDOFF_VERSION,
            "supported": supported,
            "protocols": ["a2a", "agui"] if supported else [],
            "sourceQuiesce": supported,
            "immutableSharedCommit": supported,
            "writerFence": "cooperative-runtime-local" if supported else None,
            "runningControllerTransfer": False,
            "physicalOwnerFence": supported,
            "guidanceInputJournal": supported,
        }

    def _migration_dir(self, root: Path, request: HandoffPrepareRequest) -> Path:
        key = "\0".join((request.backend_scope, request.user_id, request.session_id, request.migration_id))
        return root / hashlib.sha256(key.encode()).hexdigest()

    def _keys(self, request: HandoffPrepareRequest) -> list[str]:
        keys = [
            "context:" + request.context_id,
            SessionWriterFence.session_key(self.storage.session_dir(request.cwd, request.internal_session_id)),
            SessionWriterFence.workspace_key(request.cwd),
        ]
        if request.protocol == "agui":
            identity = request.agui_identity or {}
            if not all(
                isinstance(identity.get(name), str) and identity[name]
                for name in ("threadId", "executionId", "rosInvocationId")
            ):
                raise ValueError("AG-UI handoff requires its original execution identities")
            keys.append("thread:" + identity["threadId"])
        return keys

    async def prepare_handoff(self, request: HandoffPrepareRequest) -> HandoffResult:
        if not self.capabilities()["supported"]:
            return HandoffResult(status="UNSUPPORTED", reason="handoff_storage_not_configured")
        keys = self._keys(request)
        if self.fence.owner("context:" + request.context_id) != request.source.model_dump(mode="json", by_alias=True):
            return HandoffResult(status="NOT_READY", reason="source_physical_owner_proof_missing")
        assert self.shared_root is not None
        destination = self._migration_dir(self.shared_root, request)
        if (destination / "commit.json").exists():
            manifest, receipt = await asyncio.to_thread(self._read_committed, destination)
            if (
                receipt.model_dump(exclude=set(MigrationReceipt.model_fields) - set(HandoffPrepareRequest.model_fields))
                != request.model_dump()
            ):
                return HandoffResult(status="CONFLICT", reason="migration_identity_changed")
            return HandoffResult(status="PREPARED", receipt=receipt)
        context = await self.task_store.get_context_record(request.context_id)
        task = await self.task_store.get_task_record(request.task_id)
        if (
            context.session_id != request.internal_session_id
            or context.cwd != request.cwd
            or task.context_id != request.context_id
        ):
            return HandoffResult(status="CONFLICT", reason="protocol_session_binding_changed")
        if not await asyncio.to_thread(self.fence.prepare, keys, request.migration_id, request.epoch):
            return HandoffResult(status="CONFLICT", reason="source_epoch_changed")
        control = self.controls.get_for_context(request.context_id) if self.controls is not None else None
        snapshot = await control.observe_state() if control is not None else None
        if snapshot is None and self.persistence_root is not None:
            snapshot = A2APersistenceStore(self.persistence_root).load_execution_control(request.context_id)
        if not isinstance(snapshot, dict) or snapshot.get("taskId") != request.task_id:
            return HandoffResult(status="NOT_READY", reason="predecessor_execution_proof_missing")
        if snapshot.get("externalOperations"):
            return HandoffResult(status="NOT_READY", reason="external_operation_requires_reconciliation")
        input_ready = task.state == "input-required" and snapshot.get("inputHandoffReady") is True
        terminal_ready = snapshot.get("releaseReady") is True and task.state in {
            "completed",
            "failed",
            "canceled",
            "input-required",
        }
        if not input_ready and not terminal_ready and control is not None:
            await control.terminate(
                execution_id=control.execution_id,
                request_id="handoff-drain-" + request.migration_id,
                connection_epoch=control.connection_epoch,
                reason="sandbox_handoff",
            )
            return HandoffResult(status="NOT_READY", reason="predecessor_cancel_drain_requested")
        if control is not None and control.has_managed_work():
            return HandoffResult(status="NOT_READY", reason="predecessor_participants_draining")
        if not input_ready and not terminal_ready:
            return HandoffResult(status="NOT_READY", reason="working_checkpoint_not_transferable")
        if request.protocol == "agui":
            state = self.agui_store.load_thread((request.agui_identity or {})["threadId"])
            if not self._agui_matches(request, state):
                return HandoffResult(status="CONFLICT", reason="agui_execution_binding_changed")
        if not await asyncio.to_thread(self.fence.quiesce, keys, request.migration_id, request.epoch):
            return HandoffResult(status="NOT_READY", reason="predecessor_writers_draining")
        kind = "input_required" if input_ready else "terminal"
        receipt = await asyncio.to_thread(
            self._publish, request, snapshot, destination, kind, task.expected_permission_backup_generation
        )
        return HandoffResult(status="PREPARED", receipt=receipt)

    @staticmethod
    def _agui_matches(request: HandoffPrepareRequest, state: dict[str, Any] | None) -> bool:
        identity = request.agui_identity or {}
        if not isinstance(state, dict):
            return False
        execution = state.get("execution", {})
        return (
            state.get("threadId") == identity.get("threadId")
            and state.get("contextId") == request.context_id
            and state.get("iacCodeSessionId") == request.internal_session_id
            and state.get("cwd") == request.cwd
            and state.get("userId") == request.user_id
            and (execution.get("taskId") or execution.get("lastTaskId")) == request.task_id
            and execution.get("executionId") == identity.get("executionId")
            and execution.get("rosInvocationId") == identity.get("rosInvocationId")
        )

    def _roots(self, request: HandoffPrepareRequest) -> dict[str, Path]:
        assert self.persistence_root is not None
        roots = {
            "session": self.storage.session_dir(request.cwd, request.internal_session_id),
            "workspace": Path(request.cwd),
            "task": self.persistence_root / "tasks" / f"{_protocol_id_file_stem(request.task_id)}.json",
            "context": self.persistence_root / "contexts" / f"{_protocol_id_file_stem(request.context_id)}.json",
            "control": self.persistence_root / "execution-control" / f"{request.context_id}.json",
            "guidance": GuidanceInputJournal().context_dir(request.context_id),
        }
        if request.protocol == "agui":
            roots["agui"] = self.agui_store.path_for_thread((request.agui_identity or {})["threadId"])
        return roots

    def _input_acceptance(
        self, request: HandoffPrepareRequest, snapshot_root: Path | None = None
    ) -> Literal["APPLIED", "NOT_APPLIED_CONFIRMED", "UNKNOWN"]:
        if not request.guidance_id or not request.input_digest:
            return "UNKNOWN"
        if snapshot_root is None:
            record = GuidanceInputJournal().read(context_id=request.context_id, guidance_id=request.guidance_id)
        else:
            file = snapshot_root / "guidance" / (hashlib.sha256(request.guidance_id.encode()).hexdigest() + ".json")
            record = json.loads(file.read_bytes()) if file.is_file() else None
        if record is None:
            if snapshot_root is not None and request.input_application_unknown:
                return "UNKNOWN"
            return "NOT_APPLIED_CONFIRMED"
        if any(
            record.get(key) != value
            for key, value in {
                "context_id": request.context_id,
                "task_id": request.task_id,
                "guidance_id": request.guidance_id,
                "input_digest": request.input_digest,
            }.items()
        ):
            raise HandoffFrozenError("Guidance acceptance identity changed")
        return "APPLIED" if record.get("status") == "APPLIED" else "UNKNOWN"

    @staticmethod
    def _digest(value: Any) -> str:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def _publish(
        self,
        request: HandoffPrepareRequest,
        snapshot: dict[str, Any],
        destination: Path,
        kind: Literal["terminal", "input_required"],
        permission_generation: int | None,
    ) -> MigrationReceipt:
        assert self.shared_root is not None
        roots = self._roots(request)
        session_root = roots["session"]
        require_supported_session_layout(session_root)
        for category, root in roots.items():
            if category == "guidance" and not root.exists():
                continue
            if root.is_symlink() or not root.exists():
                raise ValueError("Required handoff state is missing or symbolic")
            if root.resolve() == self.shared_root.resolve() or self.shared_root.resolve().is_relative_to(
                root.resolve()
            ):
                raise ValueError("Shared handoff store overlaps a source directory")
        with cross_process_file_lock(destination.with_suffix(".lock")):
            if (destination / "commit.json").exists():
                return self._read_committed(destination)[1]
            temporary = destination.with_name(destination.name + ".building-" + uuid.uuid4().hex)
            temporary.mkdir(parents=True, mode=0o700)
            files: list[dict[str, Any]] = []
            try:
                for category, root in roots.items():
                    if category == "guidance" and not root.exists():
                        continue
                    entries = sorted(root.rglob("*")) if root.is_dir() else [root]
                    for entry in entries:
                        if entry.is_symlink():
                            raise ValueError("Handoff snapshots cannot contain symlinks")
                        if entry.is_dir():
                            continue
                        if not entry.is_file():
                            raise ValueError("Handoff snapshots require regular files")
                        relative = entry.relative_to(root) if root.is_dir() else Path("state.json")
                        name = (Path(category) / relative).as_posix()
                        payload = entry.read_bytes()
                        target = temporary / name
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target.write_bytes(payload)
                        files.append(
                            {"path": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
                        )
                manifest = {
                    "version": HANDOFF_VERSION,
                    "identity": request.model_dump(mode="json", by_alias=True),
                    "files": files,
                }
                digest = self._digest(manifest)
                receipt = MigrationReceipt(
                    **request.model_dump(),
                    commit_id=uuid.uuid4().hex,
                    manifest_digest=digest,
                    backup_generation=request.epoch,
                    business_revision=snapshot.get("revision", 0),
                    source_quiesced=True,
                    shared_committed=True,
                    recovery_kind=kind,
                    permission_generation=permission_generation,
                    input_acceptance=self._input_acceptance(request),
                )
                atomic_write_json(temporary / "manifest.json", manifest)
                # Visibility of this marker alone is insufficient: readers hash
                # the manifest and every payload again before accepting it.
                atomic_write_json(temporary / "commit.json", receipt.model_dump(mode="json", by_alias=True))
                temporary.rename(destination)
                self._read_committed(destination)
                return receipt
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    def _read_committed(self, directory: Path) -> tuple[dict[str, Any], MigrationReceipt]:
        if directory.is_symlink():
            raise ValueError("Invalid handoff snapshot directory")
        receipt = MigrationReceipt.model_validate_json((directory / "commit.json").read_bytes())
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if self._digest(manifest) != receipt.manifest_digest:
            raise ValueError("Handoff manifest digest mismatch")
        identity = HandoffPrepareRequest.model_validate(manifest["identity"])
        if any(getattr(receipt, name) != getattr(identity, name) for name in HandoffPrepareRequest.model_fields):
            raise ValueError("Handoff receipt identity mismatch")
        items = manifest.get("files")
        if not isinstance(items, list):
            raise ValueError("Handoff manifest has no file inventory")
        names = [item.get("path") for item in items if isinstance(item, dict)]
        required = {"session/metadata.json", "task/state.json", "context/state.json", "control/state.json"}
        if receipt.protocol == "agui":
            required.add("agui/state.json")
        if len(names) != len(items) or len(set(names)) != len(names) or not required.issubset(names):
            raise ValueError("Handoff manifest omits required state or duplicates paths")
        for item in items:
            relative = Path(item["path"])
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.parts[0] not in self._roots(receipt)
            ):
                raise ValueError("Invalid handoff manifest path")
            file = directory / relative
            if any(parent.is_symlink() for parent in [file, *file.parents] if parent != directory.parent):
                raise ValueError("Invalid handoff manifest file")
            payload = file.read_bytes()
            if len(payload) != item["size"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise ValueError("Handoff payload digest mismatch")
        return manifest, receipt

    async def discover_completed_session(self, discovery: HandoffDiscoverRequest) -> HandoffResult:
        if not self.capabilities()["supported"]:
            return HandoffResult(status="UNSUPPORTED", reason="handoff_storage_not_configured")
        request = discovery.request
        assert self.shared_root is not None
        mutable_fields = {
            "migration_id",
            "epoch",
            "pending_input",
            "input_digest",
            "guidance_id",
            "input_application_unknown",
        }
        candidates: list[tuple[dict[str, Any], MigrationReceipt, Path]] = []
        paths = (
            [self._migration_dir(self.shared_root, discovery.checkpoint)]
            if discovery.checkpoint
            else sorted(self.shared_root.glob("*/commit.json"))
        )
        for path in paths:
            directory = path if discovery.checkpoint else path.parent
            if not (directory / "commit.json").is_file():
                continue
            manifest, receipt = await asyncio.to_thread(self._read_committed, directory)
            if discovery.checkpoint and receipt != discovery.checkpoint:
                return HandoffResult(status="CONFLICT", reason="source_checkpoint_changed")
            if not discovery.checkpoint and receipt.recovery_kind != "terminal":
                continue
            if any(
                getattr(receipt, field) != getattr(request, field)
                for field in HandoffPrepareRequest.model_fields
                if field not in mutable_fields
            ):
                continue
            candidates.append((manifest, receipt, directory))
        if not candidates:
            return HandoffResult(status="NOT_READY", reason="predecessor_checkpoint_missing")
        manifest, checkpoint, directory = max(
            candidates, key=lambda item: (item[1].business_revision, item[1].backup_generation)
        )
        if request.epoch <= checkpoint.epoch or request.migration_id == checkpoint.migration_id:
            return HandoffResult(status="CONFLICT", reason="migration_epoch_must_follow_checkpoint")
        receipt = await asyncio.to_thread(self._adopt_checkpoint, request, manifest, checkpoint, directory)
        return HandoffResult(status="PREPARED", receipt=receipt)

    def _adopt_checkpoint(
        self, request: HandoffPrepareRequest, manifest: dict[str, Any], checkpoint: MigrationReceipt, source: Path
    ) -> MigrationReceipt:
        assert self.shared_root is not None
        destination = self._migration_dir(self.shared_root, request)
        with cross_process_file_lock(destination.with_suffix(".lock")):
            if (destination / "commit.json").exists():
                receipt = self._read_committed(destination)[1]
                if any(getattr(receipt, name) != getattr(request, name) for name in HandoffPrepareRequest.model_fields):
                    raise HandoffFrozenError("Adopted migration identity changed")
                return receipt
            temporary = destination.with_name(destination.name + ".building-" + uuid.uuid4().hex)
            temporary.mkdir(parents=True, mode=0o700)
            try:
                for item in manifest["files"]:
                    target = temporary / item["path"]
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source / item["path"], target)
                adopted = {
                    "version": HANDOFF_VERSION,
                    "identity": request.model_dump(mode="json", by_alias=True),
                    "files": manifest["files"],
                }
                receipt = MigrationReceipt(
                    **request.model_dump(),
                    commit_id=uuid.uuid4().hex,
                    manifest_digest=self._digest(adopted),
                    backup_generation=request.epoch,
                    business_revision=checkpoint.business_revision,
                    source_quiesced=True,
                    shared_committed=True,
                    recovery_kind=checkpoint.recovery_kind,
                    permission_generation=checkpoint.permission_generation,
                    source_checkpoint_commit_id=checkpoint.commit_id,
                    input_acceptance=self._input_acceptance(request, source),
                )
                atomic_write_json(temporary / "manifest.json", adopted)
                atomic_write_json(temporary / "commit.json", receipt.model_dump(mode="json", by_alias=True))
                temporary.rename(destination)
                self._read_committed(destination)
                return receipt
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    async def restore_handoff(self, request: HandoffRestoreRequest) -> HandoffResult:
        if not self.capabilities()["supported"]:
            return HandoffResult(status="UNSUPPORTED", reason="handoff_storage_not_configured")
        if request.target == request.receipt.source:
            return HandoffResult(status="CONFLICT", reason="destination_is_source_owner")
        assert self.shared_root is not None
        source = self._migration_dir(self.shared_root, request.receipt)
        if not (source / "commit.json").exists():
            return HandoffResult(status="NOT_READY", reason="shared_commit_not_visible")
        manifest, committed = await asyncio.to_thread(self._read_committed, source)
        if committed != request.receipt:
            return HandoffResult(status="CONFLICT", reason="shared_receipt_changed")
        stage = self._migration_dir(self.private_root, committed)
        await asyncio.to_thread(self._stage, source, stage, request)
        return HandoffResult(status="RESTORED", receipt=committed, target=request.target)

    def _stage(self, source: Path, stage: Path, request: HandoffRestoreRequest) -> None:
        with cross_process_file_lock(stage.with_suffix(".lock")):
            binding = stage / "destination.json"
            document = request.model_dump(mode="json", by_alias=True)
            if binding.exists():
                if json.loads(binding.read_bytes()) != document:
                    raise HandoffFrozenError("Destination reservation identity changed")
                return
            self.fence.wait_for_commit(self._keys(request.receipt), request.receipt.migration_id, request.receipt.epoch)
            stage.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copytree(source, stage / "snapshot", dirs_exist_ok=True)
            self._read_committed(stage / "snapshot")
            atomic_write_json(binding, document)

    async def authorize_destination(self, request: HandoffAuthorizeRequest) -> HandoffResult:
        stage = self._migration_dir(self.private_root, request.receipt)
        if not (stage / "destination.json").exists():
            return HandoffResult(status="NOT_READY", reason="destination_not_prepared")
        expected = HandoffRestoreRequest(receipt=request.receipt, target=request.target).model_dump(
            mode="json", by_alias=True
        )
        if json.loads((stage / "destination.json").read_bytes()) != expected:
            return HandoffResult(status="CONFLICT", reason="destination_reservation_changed")
        await asyncio.to_thread(self._activate, stage, request)
        reload_cache = getattr(self.task_store, "activate_handoff_snapshot", None)
        if reload_cache is not None:
            await reload_cache(context_id=request.receipt.context_id, task_id=request.receipt.task_id)
        retire = getattr(self.controls, "activate_handoff_epoch", None)
        if retire is not None:
            retire(request.receipt.context_id)
        self.fence.authorize(
            self._keys(request.receipt),
            request.receipt.migration_id,
            request.receipt.epoch,
            request.commit_id,
            request.target.model_dump(mode="json", by_alias=True),
        )
        self.agui_store = FileAguiThreadStateStore(self.agui_store.state_dir)
        return HandoffResult(
            status="AUTHORIZED", receipt=request.receipt, target=request.target, commit_id=request.commit_id
        )

    async def apply_pending_input(self, request: HandoffInputRequest) -> HandoffInputResult:
        receipt = request.receipt
        if request.input_digest != receipt.input_digest or not receipt.guidance_id:
            return HandoffInputResult(status="CONFLICT", reason="guidance_input_identity_changed")
        # Caller metadata is ephemeral and never enters an immutable receipt.
        identity_input = {key: value for key, value in request.pending_input.items() if key != "image_parts"}
        if identity_input != receipt.pending_input:
            return HandoffInputResult(status="CONFLICT", reason="guidance_payload_changed")
        stage = self._migration_dir(self.private_root, receipt)
        authorization = stage / "authorization.json"
        if not authorization.is_file():
            return HandoffInputResult(status="NOT_READY", reason="destination_not_authorized")
        committed = HandoffAuthorizeRequest.model_validate_json(authorization.read_bytes())
        if committed.receipt != receipt or committed.target != request.target:
            return HandoffInputResult(status="CONFLICT", reason="input_destination_identity_changed")
        _, staged = self._read_committed(stage / "snapshot")
        if staged != receipt:
            return HandoffInputResult(status="CONFLICT", reason="input_snapshot_changed")
        control_file = stage / "snapshot" / "control" / "state.json"
        control = json.loads(control_file.read_bytes())
        execution_id = (
            (receipt.agui_identity or {}).get("executionId")
            if receipt.protocol == "agui"
            else control.get("executionId")
        )

        if receipt.input_acceptance == "APPLIED":
            return self._input_ack(request, execution_id, "ALREADY_APPLIED")
        acceptance = self._input_acceptance(receipt)
        if acceptance == "APPLIED":
            return self._input_ack(request, execution_id, "ALREADY_APPLIED")
        if receipt.input_acceptance == "UNKNOWN" or acceptance == "UNKNOWN":
            return HandoffInputResult(status="UNKNOWN", reason="guidance_acceptance_unknown")
        if receipt.recovery_kind == "terminal":
            return HandoffInputResult(status="NOT_READY", reason="guidance_execution_already_terminal")
        if request.skip_delivery or receipt.protocol == "agui":
            return HandoffInputResult(status="NOT_READY", reason="agui_input_delivery_required")
        if self.input_delivery is None:
            return HandoffInputResult(status="NOT_READY", reason="input_delivery_not_configured")
        await self.input_delivery.apply(request)
        if self._input_acceptance(receipt) != "APPLIED":
            return HandoffInputResult(status="UNKNOWN", reason="guidance_acceptance_unconfirmed")
        return self._input_ack(request, execution_id, "APPLIED")

    def _input_ack(
        self, request: HandoffInputRequest, execution_id: str | None, status: Literal["APPLIED", "ALREADY_APPLIED"]
    ) -> HandoffInputResult:
        return HandoffInputResult(
            status=status,
            input_digest=request.input_digest,
            task_id=request.receipt.task_id,
            context_id=request.receipt.context_id,
            execution_id=execution_id,
        )

    def _publish_session_projection(self, receipt: MigrationReceipt) -> None:
        service = SessionBackupService(session_storage=self.storage)
        backup_root = service._backup_root()
        if backup_root is None:
            return
        source = self.storage.session_dir(receipt.cwd, receipt.internal_session_id)
        state = service._read_state(source, session_id=receipt.internal_session_id, missing_ok=True)
        if state is None:
            if receipt.permission_generation is not None:
                raise HandoffFrozenError("The permission checkpoint has no exact backup lineage")
            return
        if state.status != "succeeded" or (
            receipt.permission_generation is not None and state.generation < receipt.permission_generation
        ):
            raise HandoffFrozenError("The restored permission checkpoint generation is incomplete")
        destination = backup_root / "projects" / source.parent.name / receipt.internal_session_id
        with service._shared_session_lock(
            backup_root, project=source.parent.name, session_id=receipt.internal_session_id
        ):
            existing = service._read_state(
                destination, session_id=receipt.internal_session_id, shared=True, missing_ok=True
            )
            if existing is not None and (
                existing.generation > state.generation
                or (existing.generation == state.generation and existing.commit_id != state.commit_id)
            ):
                raise HandoffFrozenError("A different session lineage already occupies shared backup")
            service._mirror(source, destination)
            service._write_state(destination, state)
            confirmed = service._read_state(destination, session_id=receipt.internal_session_id, shared=True)
            if confirmed is None or not confirmed.same_lineage(state):
                raise HandoffFrozenError("The exact permission checkpoint is not visible")

    def _activate(self, stage: Path, request: HandoffAuthorizeRequest) -> None:
        with cross_process_file_lock(stage.with_suffix(".lock")):
            authorization = stage / "authorization.json"
            expected = request.model_dump(mode="json", by_alias=True)
            if authorization.exists() and json.loads(authorization.read_bytes()) != expected:
                raise HandoffFrozenError("ROS commit identity changed")
            manifest, receipt = self._read_committed(stage / "snapshot")
            if receipt != request.receipt:
                raise HandoffFrozenError("Prepared receipt changed")
            roots = self._roots(receipt)
            # Copy only after the authenticated ROS coordinator confirms its
            # durable commit. The source is already permanently quiesced.
            for entry in manifest["files"]:
                relative = Path(entry["path"])
                category = relative.parts[0]
                target = (
                    roots[category] / Path(*relative.parts[1:])
                    if category in {"session", "workspace", "guidance"}
                    else roots[category]
                )
                if any(parent.is_symlink() for parent in [target, *target.parents]):
                    raise HandoffFrozenError("Destination contains a symbolic file")
                payload = (stage / "snapshot" / relative).read_bytes()
                if target.exists() and target.read_bytes() != payload:
                    raise HandoffFrozenError("Destination has conflicting newer state")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not target.exists():
                    target.write_bytes(payload)
            self._publish_session_projection(receipt)
            atomic_write_json(authorization, expected)
