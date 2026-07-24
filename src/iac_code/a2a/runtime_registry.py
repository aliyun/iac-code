"""Process-local registry for live A2A runtime owners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class A2ARuntimeOwner:
    task_store: Any
    model: str
    metrics: Any
    persistence_root: str | Path | None = None
    artifact_store: Any | None = None
    push_notifier: Any | None = None
    permission_resolver: Any | None = None
    auto_approve_permissions: bool = False
    thinking_exposure_types: Any = None


@dataclass(frozen=True)
class A2ARuntimeRegistration:
    owner: A2ARuntimeOwner

    def unregister(self) -> None:
        unregister_runtime_owner(self.owner)


_OWNERS: list[A2ARuntimeOwner] = []


def register_runtime_owner(owner: A2ARuntimeOwner) -> A2ARuntimeRegistration:
    _OWNERS.append(owner)
    return A2ARuntimeRegistration(owner)


def unregister_runtime_owner(owner: A2ARuntimeOwner) -> None:
    for index in range(len(_OWNERS) - 1, -1, -1):
        if _OWNERS[index] is owner:
            del _OWNERS[index]
            return


def get_runtime_owner(*, persistence_root: str | Path | None = None) -> A2ARuntimeOwner | None:
    normalized_root = _normalized_root(persistence_root)
    for owner in reversed(_OWNERS):
        if normalized_root is None or _normalized_root(owner.persistence_root) == normalized_root:
            return owner
    return None


def _normalized_root(root: str | Path | None) -> Path | None:
    if root is None:
        return None
    return Path(root).expanduser().resolve()
