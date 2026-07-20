"""Loop-neutral, one-shot storage for approved Alibaba Cloud contracts."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from iac_code.tools.cloud.aliyun.api_contract import CanonicalWireContract
from iac_code.types.permissions import ExecutionClass, InvocationBinding

_DEFAULT_MAX_ENTRIES = 256
_DEFAULT_TTL_SECONDS = 15 * 60


class ResolvedContractError(ValueError):
    """A contract snapshot was absent or failed one-shot validation."""

    def __init__(self, message: str, *, lifecycle: str | None = None) -> None:
        super().__init__(message)
        self.lifecycle = lifecycle


@dataclass(frozen=True)
class ResolvedContractSnapshot:
    binding: InvocationBinding
    contract: CanonicalWireContract
    security_digest: str
    execution_class: ExecutionClass


@dataclass(frozen=True)
class ResolvedContractRecovery:
    """One-shot claim to re-resolve an evicted or expired approved contract."""

    binding: InvocationBinding
    security_digest: str
    execution_class: ExecutionClass
    claim_id: str


@dataclass(frozen=True)
class _StoredSnapshot:
    snapshot: ResolvedContractSnapshot
    expires_at: float


@dataclass(frozen=True)
class _RecoverableSnapshot:
    binding: InvocationBinding
    security_digest: str
    execution_class: ExecutionClass
    state: Literal["evicted", "expired", "recovering"]
    claim_id: str | None = None


def canonical_input_sha256(tool_input: Mapping[str, Any]) -> str:
    """Hash the complete JSON tool input without dereferencing path values."""

    encoded = json.dumps(
        dict(tool_input),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ResolvedContractStore:
    """Bounded process-safe snapshot handoff with short synchronous sections."""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        claim_token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_factory = token_factory
        self._claim_token_factory = claim_token_factory
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _StoredSnapshot] = OrderedDict()
        self._recoverable: OrderedDict[str, _RecoverableSnapshot] = OrderedDict()
        self._terminal: OrderedDict[str, str] = OrderedDict()

    @property
    def size(self) -> int:
        with self._lock:
            self._remove_expired(self._clock())
            return len(self._entries)

    def create(
        self,
        *,
        binding: InvocationBinding,
        contract: CanonicalWireContract,
        security_digest: str,
        execution_class: ExecutionClass,
    ) -> str:
        snapshot = ResolvedContractSnapshot(
            binding=binding,
            contract=contract,
            security_digest=security_digest,
            execution_class=execution_class,
        )
        with self._lock:
            now = self._clock()
            self._remove_expired(now)
            while len(self._entries) >= self._max_entries:
                evicted_id, evicted = next(iter(self._entries.items()))
                if not self._remember_recoverable(evicted_id, evicted.snapshot, state="evicted"):
                    raise ResolvedContractError("snapshot_capacity_exhausted")
                self._entries.pop(evicted_id, None)
            snapshot_id = self._unique_token()
            self._entries[snapshot_id] = _StoredSnapshot(snapshot, now + self._ttl_seconds)
            return snapshot_id

    def consume(
        self,
        *,
        snapshot_id: str,
        binding: InvocationBinding,
        security_digest: str,
    ) -> ResolvedContractSnapshot | ResolvedContractRecovery:
        with self._lock:
            stored = self._entries.get(snapshot_id)
            if stored is not None:
                snapshot = stored.snapshot
                self._validate_approval(
                    snapshot_id,
                    binding=binding,
                    security_digest=security_digest,
                    approved_binding=snapshot.binding,
                    approved_digest=snapshot.security_digest,
                )
                if stored.expires_at <= self._clock():
                    if not self._remember_recoverable(snapshot_id, snapshot, state="expired"):
                        raise ResolvedContractError("snapshot_capacity_exhausted", lifecycle="expired")
                    self._entries.pop(snapshot_id, None)
                    return self._claim_recovery(snapshot_id, self._recoverable[snapshot_id])
                self._entries.pop(snapshot_id, None)
                self._remember_terminal(snapshot_id, "consumed")
                return snapshot

            recoverable = self._recoverable.get(snapshot_id)
            if recoverable is not None:
                if recoverable.state == "recovering":
                    raise ResolvedContractError("snapshot_not_found", lifecycle="recovering")
                self._validate_approval(
                    snapshot_id,
                    binding=binding,
                    security_digest=security_digest,
                    approved_binding=recoverable.binding,
                    approved_digest=recoverable.security_digest,
                )
                return self._claim_recovery(snapshot_id, recoverable)

            terminal = self._terminal.get(snapshot_id)
            if terminal is not None:
                self._remember_terminal(snapshot_id, "replayed")
                raise ResolvedContractError("snapshot_not_found", lifecycle=terminal)
            raise ResolvedContractError("snapshot_not_found")

    def complete_recovery(
        self,
        *,
        snapshot_id: str,
        claim_id: str,
        binding: InvocationBinding,
        security_digest: str,
        execution_class: ExecutionClass,
    ) -> None:
        with self._lock:
            recoverable = self._recoverable.get(snapshot_id)
            if (
                recoverable is None
                or recoverable.state != "recovering"
                or recoverable.claim_id is None
                or not isinstance(claim_id, str)
                or not secrets.compare_digest(recoverable.claim_id, claim_id)
            ):
                lifecycle = self._terminal.get(snapshot_id)
                raise ResolvedContractError("snapshot_not_found", lifecycle=lifecycle)
            self._validate_approval(
                snapshot_id,
                binding=binding,
                security_digest=security_digest,
                approved_binding=recoverable.binding,
                approved_digest=recoverable.security_digest,
            )
            if recoverable.execution_class != execution_class:
                self._recoverable.pop(snapshot_id, None)
                self._remember_terminal(snapshot_id, "rejected")
                raise ResolvedContractError("snapshot_execution_class_mismatch", lifecycle="rejected")
            self._recoverable.pop(snapshot_id, None)
            self._remember_terminal(snapshot_id, "consumed")

    def cancel(self, snapshot_id: str) -> None:
        with self._lock:
            self._terminalize_pending(snapshot_id, "cancelled")

    def reject(self, snapshot_id: str) -> None:
        with self._lock:
            self._terminalize_pending(snapshot_id, "rejected")

    def cancel_recovery(self, snapshot_id: str, claim_id: str) -> None:
        with self._lock:
            self._terminalize_recovery(snapshot_id, claim_id, "cancelled")

    def reject_recovery(self, snapshot_id: str, claim_id: str) -> None:
        with self._lock:
            self._terminalize_recovery(snapshot_id, claim_id, "rejected")

    def is_pending(self, snapshot_id: str) -> bool:
        """Return whether a snapshot still requires owner cleanup."""

        with self._lock:
            stored = self._entries.get(snapshot_id)
            if stored is not None and stored.expires_at <= self._clock():
                if self._remember_recoverable(snapshot_id, stored.snapshot, state="expired"):
                    self._entries.pop(snapshot_id, None)
            return snapshot_id in self._entries or snapshot_id in self._recoverable

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, value in self._entries.items() if value.expires_at <= now]
        for key in expired:
            stored = self._entries.get(key)
            if stored is not None and self._remember_recoverable(key, stored.snapshot, state="expired"):
                self._entries.pop(key, None)

    def _validate_approval(
        self,
        snapshot_id: str,
        *,
        binding: InvocationBinding,
        security_digest: str,
        approved_binding: InvocationBinding,
        approved_digest: str,
    ) -> None:
        if approved_binding != binding:
            self._remember_terminal(snapshot_id, "rejected")
            raise ResolvedContractError("snapshot_binding_mismatch", lifecycle="rejected")
        if (
            not isinstance(approved_digest, str)
            or not isinstance(security_digest, str)
            or not secrets.compare_digest(approved_digest, security_digest)
        ):
            self._remember_terminal(snapshot_id, "rejected")
            raise ResolvedContractError("snapshot_digest_mismatch", lifecycle="rejected")

    def _claim_recovery(
        self,
        snapshot_id: str,
        recoverable: _RecoverableSnapshot,
    ) -> ResolvedContractRecovery:
        claim_id = self._unique_claim_token()
        claimed = _RecoverableSnapshot(
            binding=recoverable.binding,
            security_digest=recoverable.security_digest,
            execution_class=recoverable.execution_class,
            state="recovering",
            claim_id=claim_id,
        )
        self._recoverable[snapshot_id] = claimed
        self._recoverable.move_to_end(snapshot_id)
        return ResolvedContractRecovery(
            binding=claimed.binding,
            security_digest=claimed.security_digest,
            execution_class=claimed.execution_class,
            claim_id=claim_id,
        )

    def _remember_recoverable(
        self,
        snapshot_id: str,
        snapshot: ResolvedContractSnapshot,
        *,
        state: Literal["evicted", "expired"],
    ) -> bool:
        if snapshot_id in self._terminal:
            return False
        self._recoverable.pop(snapshot_id, None)
        while len(self._recoverable) >= self._max_entries:
            removable = next(
                (key for key, value in self._recoverable.items() if value.state != "recovering"),
                None,
            )
            if removable is None:
                return False
            self._recoverable.pop(removable, None)
        self._recoverable[snapshot_id] = _RecoverableSnapshot(
            binding=snapshot.binding,
            security_digest=snapshot.security_digest,
            execution_class=snapshot.execution_class,
            state=state,
        )
        return True

    def _remember_terminal(self, snapshot_id: str, state: str) -> None:
        self._entries.pop(snapshot_id, None)
        self._recoverable.pop(snapshot_id, None)
        self._terminal.pop(snapshot_id, None)
        self._terminal[snapshot_id] = state
        while len(self._terminal) > self._max_entries:
            self._terminal.popitem(last=False)

    def _terminalize_pending(self, snapshot_id: str, state: str) -> None:
        removed = self._entries.pop(snapshot_id, None) is not None
        recoverable = self._recoverable.get(snapshot_id)
        if recoverable is not None and recoverable.state != "recovering":
            self._recoverable.pop(snapshot_id, None)
            removed = True
        if removed:
            self._remember_terminal(snapshot_id, state)

    def _terminalize_recovery(self, snapshot_id: str, claim_id: str, state: str) -> None:
        recoverable = self._recoverable.get(snapshot_id)
        if (
            recoverable is None
            or recoverable.state != "recovering"
            or recoverable.claim_id is None
            or not isinstance(claim_id, str)
            or not secrets.compare_digest(recoverable.claim_id, claim_id)
        ):
            return
        self._recoverable.pop(snapshot_id, None)
        self._remember_terminal(snapshot_id, state)

    def _unique_token(self) -> str:
        for _ in range(16):
            candidate = self._token_factory()
            if (
                candidate
                and candidate not in self._entries
                and candidate not in self._recoverable
                and candidate not in self._terminal
            ):
                return candidate
        raise RuntimeError("snapshot_id_generation_failed")

    def _unique_claim_token(self) -> str:
        active_claims = {value.claim_id for value in self._recoverable.values() if value.claim_id is not None}
        for _ in range(16):
            candidate = self._claim_token_factory()
            if candidate and candidate not in active_claims:
                return candidate
        raise RuntimeError("snapshot_claim_generation_failed")


PROCESS_RESOLVED_CONTRACT_STORE = ResolvedContractStore()
