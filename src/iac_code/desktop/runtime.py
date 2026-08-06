"""Validated values supplied by the native Desktop host."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

DistributionChannel = Literal["macos", "windows", "appimage", "deb", "development"]
UpdateMode = Literal["tauri", "external"]


@dataclass(frozen=True)
class DesktopInstallContext:
    """Desktop-owned paths and startup state exposed to the Web adapter.

    This deliberately excludes business configuration and credentials.  The native
    host owns these paths and passes their resolved values to the sidecar.
    """

    install_id: str
    runtime_dir: Path
    host_state_dir: Path
    install_lock_dir: Path
    sidecar_generation: int = 0
    host_capture_path: Path | None = None
    python_log_path: Path | None = None
    degraded_prerequisites: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        allowed_install_id_characters = "abcdefghijklmnopqrstuvwxyz0123456789-"
        if not self.install_id or any(character not in allowed_install_id_characters for character in self.install_id):
            raise ValueError("desktop install id must contain only lowercase ASCII letters, digits, and hyphens")
        for path in (self.runtime_dir, self.host_state_dir, self.install_lock_dir):
            if not path.is_absolute():
                raise ValueError("desktop runtime paths must be absolute")
        if self.sidecar_generation < 0:
            raise ValueError("sidecar generation cannot be negative")

    def public_payload(self) -> dict[str, Any]:
        return {
            "runtimeDir": str(self.runtime_dir),
            "hostStateDir": str(self.host_state_dir),
            "installLockDir": str(self.install_lock_dir),
            "hostCapturePath": str(self.host_capture_path) if self.host_capture_path else None,
            "pythonLogPath": str(self.python_log_path) if self.python_log_path else None,
        }


@dataclass(frozen=True)
class DesktopRuntimeConfig:
    default_project_cwd: Path
    distribution_channel: DistributionChannel
    update_mode: UpdateMode
    install_context: DesktopInstallContext

    def __post_init__(self) -> None:
        project = self.default_project_cwd
        if not project.is_absolute() or not project.is_dir():
            raise ValueError("default_project_cwd must be an existing absolute directory")
        if self.distribution_channel not in {"macos", "windows", "appimage", "deb", "development"}:
            raise ValueError("unsupported Desktop distribution channel")
        if self.update_mode not in {"tauri", "external"}:
            raise ValueError("unsupported Desktop update mode")
        native_updater_supported = self.distribution_channel in {"macos", "windows", "appimage", "development"}
        if self.update_mode == "tauri" and not native_updater_supported:
            raise ValueError("distribution channel and update mode are inconsistent")

    @property
    def capabilities(self) -> Mapping[str, bool]:
        return {
            "nativeProjectPicker": True,
            "nativeSecretConfirmation": True,
            "nativeRestart": True,
            "nativeUpdater": self.update_mode == "tauri",
        }

    def bootstrap_payload(self, *, current_project_cwd: Path | None = None) -> dict[str, Any]:
        project = current_project_cwd or self.default_project_cwd
        return {
            "runtime": "desktop",
            "capabilities": dict(self.capabilities),
            "distributionChannel": self.distribution_channel,
            "updateMode": self.update_mode,
            "degradedPrerequisites": list(self.install_context.degraded_prerequisites),
            "defaultProjectCwd": str(project),
        }
