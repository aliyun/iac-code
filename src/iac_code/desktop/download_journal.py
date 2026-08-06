"""Crash-recoverable Desktop transactions for managed prerequisite downloads."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iac_code.desktop.external_env import guarded_command, run_external, spawn_env
from iac_code.desktop.install_lock import DesktopInstallLease
from iac_code.desktop.runtime import DesktopInstallContext

_COMPLETE = "post_install_complete"
_PHASES = {
    "downloading",
    "replace_pending",
    "replaced_pending_validation",
    "validated_pending_post_install",
    _COMPLETE,
}
_CONSUMER_IMPACTS = {"artifact", "prerequisite"}
DESKTOP_RECOVERY_TIMEOUT_SECONDS = 360.0


class DesktopRecoveryRequiredError(RuntimeError):
    """An unfinished managed install must be repaired before it is replaced."""


@dataclass(frozen=True)
class DesktopPrerequisiteRecipe:
    """Current-bundle allowlisted recovery recipe; journals never supply commands."""

    prerequisite: str
    consumer_impact: str
    installer_id: str
    final_path: Path
    platform_name: str
    expected_sha256: str
    minimum_version: str
    version_command: tuple[str, ...]
    version_pattern: str
    post_install_commands: tuple[tuple[str, ...], ...]
    post_install_timeout: float
    executable_mode: int = 0o755

    def __post_init__(self) -> None:
        if self.consumer_impact not in _CONSUMER_IMPACTS:
            raise ValueError("unsupported Desktop prerequisite consumer impact")
        if not self.final_path.is_absolute():
            raise ValueError("Desktop prerequisite recipe target must be absolute")
        if not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
            raise ValueError("Desktop prerequisite recipe sha256 is invalid")
        if not self.version_command or not self.post_install_commands:
            raise ValueError("Desktop prerequisite recovery recipe is incomplete")

    @property
    def fingerprint(self) -> str:
        payload = {
            "consumerImpact": self.consumer_impact,
            "executableMode": self.executable_mode,
            "finalPath": str(self.final_path.resolve()),
            "installerId": self.installer_id,
            "minimumVersion": self.minimum_version,
            "platform": self.platform_name,
            "postInstallCommands": self.post_install_commands,
            "postInstallTimeout": self.post_install_timeout,
            "prerequisite": self.prerequisite,
            "versionCommand": self.version_command,
            "versionPattern": self.version_pattern,
        }
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _normalized_platform(value: str) -> str:
    normalized = value.strip().lower()
    return {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(normalized, normalized)


def _normalized_architecture(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"x86_64", "amd64"}:
        return "amd64"
    if normalized in {"aarch64", "arm64"}:
        return "arm64"
    return normalized


def infraguard_recovery_recipe(
    raw_prerequisite: Mapping[str, Any],
    *,
    platform_system: str | None = None,
    platform_machine: str | None = None,
    home: Path | None = None,
) -> DesktopPrerequisiteRecipe:
    """Build the fixed InfraGuard recipe from the current bundled pipeline manifest."""
    current_platform = _normalized_platform(platform_system or platform.system())
    current_architecture = _normalized_architecture(platform_machine or platform.machine())
    installers = raw_prerequisite.get("installers")
    if not isinstance(installers, list):
        raise ValueError("bundled infraguard installers are missing")
    installer = next(
        (
            value
            for value in installers
            if isinstance(value, Mapping)
            and value.get("id") == "direct-binary"
            and current_platform in value.get("platforms", ())
        ),
        None,
    )
    if installer is None:
        raise ValueError("bundled infraguard direct installer is unavailable")
    download = installer.get("download")
    if not isinstance(download, Mapping):
        raise ValueError("bundled infraguard download recipe is missing")
    assets = download.get("assets")
    if not isinstance(assets, list):
        raise ValueError("bundled infraguard assets are missing")
    asset = next(
        (
            value
            for value in assets
            if isinstance(value, Mapping)
            and current_platform in value.get("platforms", ())
            and current_architecture in value.get("architectures", ())
        ),
        None,
    )
    if asset is None:
        raise ValueError("bundled infraguard asset does not support this platform")
    version = raw_prerequisite.get("version_check")
    post_install = raw_prerequisite.get("post_install")
    if not isinstance(version, Mapping) or not isinstance(post_install, Mapping):
        raise ValueError("bundled infraguard validation recipe is missing")
    consumer_impact = raw_prerequisite.get("consumerImpact")
    if consumer_impact not in _CONSUMER_IMPACTS:
        raise ValueError("bundled infraguard consumer impact is missing or invalid")
    installed_name = str(download.get("installed_name") or "infraguard")
    if current_platform == "windows" and not installed_name.lower().endswith(".exe"):
        installed_name += ".exe"
    configured_dir = Path(str(download.get("install_dir") or "~/bin"))
    install_dir = (home / "bin") if str(configured_dir) == "~/bin" and home is not None else configured_dir.expanduser()
    command = tuple(str(value) for value in version.get("command") or ("infraguard", "version"))
    commands = tuple(tuple(str(value) for value in item) for item in post_install.get("commands") or ())
    return DesktopPrerequisiteRecipe(
        prerequisite="infraguard",
        consumer_impact=str(consumer_impact),
        installer_id="direct-binary",
        final_path=(install_dir / installed_name).resolve(),
        platform_name=current_platform,
        expected_sha256=str(asset.get("sha256") or "").lower(),
        minimum_version=str(version.get("minimum") or ""),
        version_command=command,
        version_pattern=str(version.get("pattern") or r"(?P<version>\d+(?:\.\d+){1,3})"),
        post_install_commands=commands,
        post_install_timeout=float(post_install.get("timeout_seconds") or 300),
    )


def current_infraguard_recovery_recipe() -> DesktopPrerequisiteRecipe:
    # Import lazily: startup recovery reads the bundled selling manifest and is
    # deliberately independent of a user's currently selected pipeline.
    import yaml

    from iac_code.pipeline import discover_pipelines

    pipeline_dir = discover_pipelines().get("selling")
    if pipeline_dir is None:
        raise ValueError("bundled selling pipeline is missing")
    manifest = yaml.safe_load((pipeline_dir / "pipeline.yaml").read_text(encoding="utf-8")) or {}
    prerequisites = manifest.get("prerequisites") if isinstance(manifest, Mapping) else None
    raw = prerequisites.get("infraguard") if isinstance(prerequisites, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ValueError("bundled infraguard prerequisite is missing")
    return infraguard_recovery_recipe(raw)


def _key(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()


def install_lock_key(path: Path) -> str:
    """Public, canonical lock-file key for a Desktop prerequisite target.

    Callers that must contend on the same lock a lease uses (tests, tooling)
    should derive the ``<key>.lock`` name through this helper. The ``normcase``
    normalization matters on Windows, where a hand-rolled ``sha256`` of a bare
    ``resolve()`` diverges from the lease's key and silently locks a different
    file — so a shared lease would appear not to block an exclusive writer.
    """
    return _key(path)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    _atomic_bytes(path, encoded)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    with temporary.open("xb") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("Desktop prerequisite journal must contain an object")
    if value.get("phase") not in _PHASES:
        raise ValueError("Desktop prerequisite journal has an invalid phase")
    return value


def _record_paths(record: dict[str, Any]) -> tuple[Path, Path]:
    final_raw = record.get("finalPath")
    temporary_raw = record.get("temporaryPath")
    if not isinstance(final_raw, str) or not final_raw or not isinstance(temporary_raw, str) or not temporary_raw:
        raise ValueError("Desktop prerequisite journal paths are invalid")
    final_path = Path(final_raw)
    temporary = Path(temporary_raw)
    if not final_path.is_absolute() or not temporary.is_absolute() or temporary.parent != final_path.parent:
        raise ValueError("Desktop prerequisite journal paths are outside the managed directory")
    install_id = record.get("desktopInstallId")
    generation = record.get("sidecarGeneration")
    if not isinstance(install_id, str) or not re.fullmatch(r"[a-z0-9-]+", install_id):
        raise ValueError("Desktop prerequisite journal install id is invalid")
    if not isinstance(generation, int) or generation < 0:
        raise ValueError("Desktop prerequisite journal generation is invalid")
    expected_name = ".{}.iac-desktop-{}-{}.download".format(final_path.name, install_id, generation)
    if temporary.name != expected_name:
        raise ValueError("Desktop prerequisite journal temporary path is invalid")
    return final_path.resolve(), temporary.resolve()


def _sha256(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Desktop prerequisite recovery timed out")
            digest.update(chunk)
    return digest.hexdigest()


class DesktopDownloadTransaction:
    def __init__(
        self,
        context: DesktopInstallContext,
        final_path: Path,
        *,
        recipe: DesktopPrerequisiteRecipe | None = None,
        force_repair: bool = False,
        lease_already_held: bool = False,
    ) -> None:
        self.context = context
        self.final_path = final_path.expanduser().resolve()
        key = _key(self.final_path)
        self.journal_path = context.install_lock_dir / "{}.transaction.json".format(key)
        self.lease = DesktopInstallLease(context.install_lock_dir / "{}.lock".format(key), timeout=30)
        self.recipe = recipe
        self.force_repair = force_repair
        self.lease_already_held = lease_already_held
        self.record: dict[str, Any] | None = None
        self._repair_record_bytes: bytes | None = None
        self._repair_snapshot_taken = False

    def __enter__(self) -> DesktopDownloadTransaction:
        if not self.lease_already_held:
            self.lease.__enter__()
        return self

    def __exit__(self, kind, value, traceback) -> None:
        if not self.lease_already_held:
            self.lease.__exit__(kind, value, traceback)

    def begin(
        self,
        installed_path: Path,
        *,
        installer_id: str,
        expected_sha256: str,
        platform_name: str,
    ) -> Path:
        installed_path = installed_path.expanduser().resolve()
        if installed_path != self.final_path:
            raise ValueError("Desktop prerequisite transaction target changed")
        if self.recipe is not None and (
            installed_path != self.recipe.final_path.resolve()
            or installer_id != self.recipe.installer_id
            or expected_sha256.lower() != self.recipe.expected_sha256
            or platform_name != self.recipe.platform_name
        ):
            raise ValueError("Desktop prerequisite installer does not match the bundled recovery recipe")
        if self.force_repair and not self._repair_snapshot_taken:
            try:
                self._repair_record_bytes = self.journal_path.read_bytes()
            except FileNotFoundError:
                self._repair_record_bytes = None
            self._repair_snapshot_taken = True
        try:
            old = self.load()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            if not self.force_repair:
                raise
            self.journal_path.unlink(missing_ok=True)
            old = None
        if old is not None:
            old_final, old_temp = _record_paths(old)
            if old_final != self.final_path:
                raise DesktopRecoveryRequiredError("Desktop prerequisite journal target does not match the installer")
            if old.get("phase") in {"downloading", _COMPLETE} or self.force_repair:
                old_temp.unlink(missing_ok=True)
                self.journal_path.unlink(missing_ok=True)
            else:
                raise DesktopRecoveryRequiredError("Desktop prerequisite installation requires recovery")
        temporary = self.final_path.with_name(
            ".{}.iac-desktop-{}-{}.download".format(
                self.final_path.name,
                self.context.install_id,
                self.context.sidecar_generation,
            )
        )
        temporary.unlink(missing_ok=True)
        recipe = self.recipe
        now = time.time()
        self.record = {
            "desktopInstallId": self.context.install_id,
            "sidecarGeneration": self.context.sidecar_generation,
            "prerequisite": recipe.prerequisite if recipe else "infraguard",
            "consumerImpact": recipe.consumer_impact if recipe else "prerequisite",
            "installerId": installer_id,
            "expectedSha256": expected_sha256.lower(),
            "expectedVersion": recipe.minimum_version if recipe else None,
            "recipeFingerprint": recipe.fingerprint if recipe else None,
            "platform": platform_name,
            "finalPath": str(self.final_path),
            "temporaryPath": str(temporary),
            "phase": "downloading",
            "createdAt": now,
            "updatedAt": now,
        }
        self._persist()
        return temporary

    def transition(self, phase: str) -> None:
        if phase not in _PHASES:
            raise ValueError("Desktop prerequisite journal phase is invalid")
        if self.record is None:
            return
        if phase == "replaced_pending_validation":
            _sync_directory(self.final_path.parent)
        self.record["phase"] = phase
        self.record["updatedAt"] = time.time()
        self._persist()

    def cancel_before_replace(self) -> None:
        record = self.record or self.load()
        if record is None or record.get("phase") != "downloading":
            return
        _final, temporary = _record_paths(record)
        temporary.unlink(missing_ok=True)
        self.journal_path.unlink(missing_ok=True)
        if self._repair_record_bytes is not None:
            _atomic_bytes(self.journal_path, self._repair_record_bytes)
        self._repair_record_bytes = None
        self._repair_snapshot_taken = False
        self.record = None
        _sync_directory(self.journal_path.parent)

    def complete(self) -> None:
        if self.record is None:
            return
        self.transition(_COMPLETE)
        self.journal_path.unlink(missing_ok=True)
        self.record = None
        self._repair_record_bytes = None
        self._repair_snapshot_taken = False
        _sync_directory(self.journal_path.parent)

    def load(self) -> dict[str, Any] | None:
        return _load_record(self.journal_path)

    def _persist(self) -> None:
        assert self.record is not None
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.journal_path, self.record)


RecoveryRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _run_recovery_command(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return run_external(
        guarded_command(list(command), kind="prerequisite"),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        env=spawn_env(),
        timeout=timeout,
    )


def _replace_command_binary(command: tuple[str, ...], recipe: DesktopPrerequisiteRecipe) -> list[str]:
    values = list(command)
    if values and values[0] == recipe.prerequisite:
        values[0] = str(recipe.final_path)
    return values


def _version_is_current(output: str, recipe: DesktopPrerequisiteRecipe) -> bool:
    match = re.search(recipe.version_pattern, output)
    if match is None:
        return False
    actual = match.groupdict().get("version") or (match.group(1) if match.lastindex else match.group(0))

    def parts(value: str) -> tuple[int, ...]:
        found = re.match(r"v?(\d+(?:\.\d+)*)", value.strip())
        return tuple(int(part) for part in found.group(1).split(".")) if found else (0,)

    actual_parts = parts(actual)
    minimum_parts = parts(recipe.minimum_version)
    width = max(len(actual_parts), len(minimum_parts))
    return actual_parts + (0,) * (width - len(actual_parts)) >= minimum_parts + (0,) * (width - len(minimum_parts))


def _validate_record_recipe(
    key: str,
    record: dict[str, Any],
    recipes: Mapping[tuple[str, str], DesktopPrerequisiteRecipe],
) -> tuple[DesktopPrerequisiteRecipe, Path, Path]:
    final_path, temporary = _record_paths(record)
    if _key(final_path) != key:
        raise ValueError("Desktop prerequisite journal key does not match its target")
    prerequisite = record.get("prerequisite")
    if not isinstance(prerequisite, str):
        raise ValueError("Desktop prerequisite journal prerequisite is invalid")
    recipe = recipes.get((prerequisite, os.path.normcase(str(final_path))))
    if recipe is None:
        raise DesktopRecoveryRequiredError("Desktop prerequisite recipe is no longer recognized")
    if (
        record.get("consumerImpact") != recipe.consumer_impact
        or record.get("installerId") != recipe.installer_id
        or record.get("expectedSha256") != recipe.expected_sha256
        or record.get("expectedVersion") != recipe.minimum_version
        or record.get("recipeFingerprint") != recipe.fingerprint
        or record.get("platform") != recipe.platform_name
    ):
        raise DesktopRecoveryRequiredError("Desktop prerequisite recipe changed")
    return recipe, final_path, temporary


def _recover_record(
    journal: Path,
    key: str,
    record: dict[str, Any],
    recipes: Mapping[tuple[str, str], DesktopPrerequisiteRecipe],
    runner: RecoveryRunner,
    deadline: float,
) -> None:
    final_path, temporary = _record_paths(record)
    if _key(final_path) != key:
        raise ValueError("Desktop prerequisite journal key does not match its target")
    phase = record["phase"]
    if phase == _COMPLETE:
        journal.unlink(missing_ok=True)
        return
    if phase == "downloading":
        temporary.unlink(missing_ok=True)
        journal.unlink(missing_ok=True)
        return
    recipe, final_path, temporary = _validate_record_recipe(key, record, recipes)
    if phase == "replace_pending":
        if temporary.exists():
            if _sha256(temporary, deadline=deadline) != recipe.expected_sha256:
                raise DesktopRecoveryRequiredError("Desktop prerequisite temporary artifact failed validation")
            if os.name != "nt":
                temporary.chmod(recipe.executable_mode)
            # Windows maps fsync to FlushFileBuffers, which needs a writable
            # handle. Keep the durability barrier on every platform by opening
            # the already-downloaded artifact for update without modifying it.
            with temporary.open("r+b") as file:
                os.fsync(file.fileno())
            temporary.replace(final_path)
            _sync_directory(final_path.parent)
        elif not final_path.exists() or _sha256(final_path, deadline=deadline) != recipe.expected_sha256:
            raise DesktopRecoveryRequiredError("Desktop prerequisite replacement cannot be proven complete")
        record["phase"] = "replaced_pending_validation"
        record["updatedAt"] = time.time()
        _atomic_json(journal, record)
        phase = record["phase"]
    if phase == "replaced_pending_validation":
        if not final_path.exists() or _sha256(final_path, deadline=deadline) != recipe.expected_sha256:
            raise DesktopRecoveryRequiredError("Desktop prerequisite installed artifact failed validation")
        if os.name != "nt":
            final_path.chmod(recipe.executable_mode)
            with final_path.open("rb") as file:
                os.fsync(file.fileno())
        remaining = min(30.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Desktop prerequisite recovery timed out")
        result = runner(_replace_command_binary(recipe.version_command, recipe), remaining)
        if result.returncode != 0 or not _version_is_current("\n".join((result.stdout, result.stderr)), recipe):
            raise DesktopRecoveryRequiredError("Desktop prerequisite version validation failed")
        record["phase"] = "validated_pending_post_install"
        record["updatedAt"] = time.time()
        _atomic_json(journal, record)
        phase = record["phase"]
    if phase == "validated_pending_post_install":
        for command in recipe.post_install_commands:
            remaining = min(recipe.post_install_timeout, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("Desktop prerequisite recovery timed out")
            result = runner(_replace_command_binary(command, recipe), remaining)
            if result.returncode != 0:
                raise DesktopRecoveryRequiredError("Desktop prerequisite post-install recovery failed")
        record["phase"] = _COMPLETE
        record["updatedAt"] = time.time()
        _atomic_json(journal, record)
        journal.unlink(missing_ok=True)


def recover_install_transactions(
    context: DesktopInstallContext,
    *,
    recipes: Sequence[DesktopPrerequisiteRecipe] | None = None,
    run_command: RecoveryRunner | None = None,
) -> tuple[str, ...]:
    """Recover safe phases and report only known prerequisite ids as degraded."""
    degraded: set[str] = set()
    context.install_lock_dir.mkdir(parents=True, exist_ok=True)
    if recipes is None:
        try:
            recipes = (current_infraguard_recovery_recipe(),)
        except (OSError, TypeError, ValueError):
            recipes = ()
    recipe_map = {
        (recipe.prerequisite, os.path.normcase(str(recipe.final_path.resolve()))): recipe for recipe in recipes
    }
    runner = run_command or _run_recovery_command
    deadline = time.monotonic() + DESKTOP_RECOVERY_TIMEOUT_SECONDS
    for journal in context.install_lock_dir.glob("*.transaction.json"):
        prerequisite = "infraguard"  # the only currently managed Desktop prerequisite
        try:
            key = journal.name.removesuffix(".transaction.json")
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                raise ValueError("Desktop prerequisite journal key is invalid")
            lock_timeout = min(5.0, deadline - time.monotonic())
            if lock_timeout <= 0:
                raise TimeoutError("Desktop prerequisite recovery timed out")
            with DesktopInstallLease(context.install_lock_dir / "{}.lock".format(key), timeout=lock_timeout):
                record = _load_record(journal)
                if record is None:
                    continue
                if isinstance(record.get("prerequisite"), str):
                    prerequisite = str(record["prerequisite"])
                _recover_record(journal, key, record, recipe_map, runner, deadline)
        except (DesktopRecoveryRequiredError, OSError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
            # Never surface a literal "unknown" card.  Unparseable legacy records
            # belong to the only managed recipe and are repaired explicitly.
            degraded.add(prerequisite if prerequisite == "infraguard" else "infraguard")
    return tuple(sorted(degraded))


def _journal_key_from_name(path: Path) -> str | None:
    key = path.name.removesuffix(".transaction.json")
    return key if re.fullmatch(r"[0-9a-f]{64}", key) else None


def has_recovery_required(
    context: DesktopInstallContext,
    *,
    prerequisite: str | None = None,
    final_path: Path | None = None,
) -> bool:
    """Return recovery state scoped to a prerequisite and/or exact final path."""
    expected_key = _key(final_path.expanduser().resolve()) if final_path is not None else None
    for journal in context.install_lock_dir.glob("*.transaction.json"):
        key = _journal_key_from_name(journal)
        if expected_key is not None and key != expected_key:
            continue
        try:
            record = _load_record(journal)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return prerequisite in {None, "infraguard"}
        if record is None:
            continue
        if prerequisite is not None and record.get("prerequisite") != prerequisite:
            continue
        return True
    return False


class DesktopTransactionReader:
    """Hold a shared lease while a Desktop probe inspects one managed artifact."""

    def __init__(self, context: DesktopInstallContext, final_path: Path, *, timeout: float = 0.0) -> None:
        self.context = context
        self.final_path = final_path.expanduser().resolve()
        key = _key(self.final_path)
        self.journal_path = context.install_lock_dir / "{}.transaction.json".format(key)
        self.timeout = timeout
        self._leases: list[DesktopInstallLease] = []

    def __enter__(self) -> DesktopTransactionReader:
        keys = {_key(self.final_path)}
        for journal in self.context.install_lock_dir.glob("*.transaction.json"):
            key = _journal_key_from_name(journal)
            if key is not None:
                keys.add(key)
        try:
            for key in sorted(keys):
                lease = DesktopInstallLease(
                    self.context.install_lock_dir / "{}.lock".format(key),
                    timeout=self.timeout,
                    shared=True,
                )
                lease.__enter__()
                self._leases.append(lease)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, kind, value, traceback) -> None:
        while self._leases:
            self._leases.pop().__exit__(kind, value, traceback)

    def recovery_required(self, *, prerequisite: str | None = "infraguard") -> bool:
        if prerequisite == "infraguard":
            # InfraGuard is prerequisite-scoped because policy update mutates its
            # shared state; an N-1 journal at an old final path still blocks it.
            return has_recovery_required(self.context, prerequisite=prerequisite)
        return has_recovery_required(self.context, prerequisite=prerequisite, final_path=self.final_path)


class DesktopPrerequisiteConsumerLease:
    """Shared leases for a real Desktop prerequisite consumer, including stale keys."""

    def __init__(
        self,
        install_lock_dir: Path,
        final_path: Path,
        *,
        prerequisite: str,
        timeout: float,
    ) -> None:
        self.install_lock_dir = install_lock_dir.expanduser().resolve()
        self.final_path = final_path.expanduser().resolve()
        self.prerequisite = prerequisite
        self.timeout = timeout
        self._leases: list[DesktopInstallLease] = []

    def __enter__(self) -> DesktopPrerequisiteConsumerLease:
        self.install_lock_dir.mkdir(parents=True, exist_ok=True)
        keys = {_key(self.final_path)}
        for journal in self.install_lock_dir.glob("*.transaction.json"):
            key = _journal_key_from_name(journal)
            if key is not None:
                keys.add(key)
        try:
            for key in sorted(keys):
                lease = DesktopInstallLease(
                    self.install_lock_dir / "{}.lock".format(key),
                    timeout=self.timeout,
                    shared=True,
                )
                lease.__enter__()
                self._leases.append(lease)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, kind, value, traceback) -> None:
        while self._leases:
            self._leases.pop().__exit__(kind, value, traceback)

    def recovery_required(self) -> bool:
        for journal in self.install_lock_dir.glob("*.transaction.json"):
            try:
                record = _load_record(journal)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return self.prerequisite == "infraguard"
            if record is not None and record.get("prerequisite") == self.prerequisite:
                impact = record.get("consumerImpact")
                if impact == "prerequisite":
                    return True
                if impact == "artifact" and record.get("finalPath") == str(self.final_path):
                    return True
                if impact not in _CONSUMER_IMPACTS:
                    return True
        return False


class DesktopPrerequisiteRepairLease:
    """Exclusive, stable-order lease set for old and current recipe keys."""

    def __init__(self, context: DesktopInstallContext, current_final_path: Path, *, timeout: float = 30.0) -> None:
        self.context = context
        self.current_final_path = current_final_path.expanduser().resolve()
        self.timeout = timeout
        self._leases: list[DesktopInstallLease] = []

    def __enter__(self) -> DesktopPrerequisiteRepairLease:
        self.context.install_lock_dir.mkdir(parents=True, exist_ok=True)
        keys = {_key(self.current_final_path)}
        for journal in self.context.install_lock_dir.glob("*.transaction.json"):
            key = _journal_key_from_name(journal)
            if key is not None:
                keys.add(key)
        try:
            for key in sorted(keys):
                lease = DesktopInstallLease(
                    self.context.install_lock_dir / "{}.lock".format(key),
                    timeout=self.timeout,
                )
                lease.__enter__()
                self._leases.append(lease)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, kind, value, traceback) -> None:
        while self._leases:
            self._leases.pop().__exit__(kind, value, traceback)


def snapshot_repair_records(context: DesktopInstallContext) -> dict[Path, bytes]:
    """Snapshot records that an explicit Desktop repair may retire after success."""
    snapshot: dict[Path, bytes] = {}
    for journal in context.install_lock_dir.glob("*.transaction.json"):
        try:
            snapshot[journal] = journal.read_bytes()
        except OSError:
            continue
    return snapshot


def clear_repaired_records(
    context: DesktopInstallContext,
    snapshot: Mapping[Path, bytes],
    *,
    locks_already_held: bool = False,
) -> None:
    """Remove only unchanged records superseded by a successful current recipe.

    Comparing the exact bytes after taking the corresponding exclusive lease
    prevents a repair in one channel from deleting a newer transaction created by
    another channel after the repair started.
    """
    for journal, expected in snapshot.items():
        key = _journal_key_from_name(journal)
        if key is None:
            try:
                if journal.read_bytes() == expected:
                    journal.unlink(missing_ok=True)
                    _sync_directory(journal.parent)
            except FileNotFoundError:
                pass
            continue
        lease = (
            None
            if locks_already_held
            else DesktopInstallLease(context.install_lock_dir / "{}.lock".format(key), timeout=5)
        )
        if lease is not None:
            lease.__enter__()
        try:
            try:
                current = journal.read_bytes()
            except FileNotFoundError:
                continue
            if current == expected:
                journal.unlink(missing_ok=True)
                _sync_directory(journal.parent)
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)
