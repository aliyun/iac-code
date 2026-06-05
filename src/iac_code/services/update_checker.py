from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from iac_code.config import get_config_dir

try:
    _fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None


_STATE_LOCK = threading.Lock()
_STATE_FILE_NAME = "update-state.yml"
_LOGGER = logging.getLogger(__name__)
OFFICIAL_PYPI_SOURCE = "official_pypi"
CONFIGURED_PIP_SOURCE = "configured_pip"
DEFAULT_RELEASE_NOTES_URL = "https://github.com/aliyun/iac-code/releases/latest"
CHECK_THROTTLE_SECONDS = 2 * 60 * 60
_PYPI_JSON_URL = "https://pypi.org/pypi/iac-code/json"
_PYPI_SIMPLE_INDEX_URL = "https://pypi.org/simple"
_GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/aliyun/iac-code/releases/latest"


@dataclass(frozen=True)
class PendingUpdate:
    version: str
    current_version: str
    source: str
    checked_at: float
    update_command: tuple[str, ...]
    release_notes_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "update_command", tuple(str(part) for part in self.update_command))


@dataclass(frozen=True)
class UpdateState:
    pending: PendingUpdate | None = None
    last_successful_check_at: float | None = None
    skip_until_version: str | None = None


def get_update_state_path() -> Path:
    return get_config_dir() / _STATE_FILE_NAME


def load_update_state(path: Path | None = None) -> UpdateState:
    state_path = path or get_update_state_path()
    if not state_path.exists():
        return UpdateState()

    try:
        data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    except Exception:
        _LOGGER.debug("Failed to load update state", exc_info=True)
        return UpdateState()

    if not isinstance(data, dict):
        return UpdateState()

    return _state_from_mapping(data)


def get_pending_update(path: Path | None = None, current_version: str | None = None) -> PendingUpdate | None:
    from iac_code import __version__

    state = load_update_state(path)
    pending = state.pending
    if pending is None:
        return None

    baseline_version = current_version or __version__
    if not _is_newer_version(pending.version, baseline_version):
        return None
    if state.skip_until_version and not _is_newer_version(pending.version, state.skip_until_version):
        return None
    return pending


def suppress_version(version: str, path: Path | None = None) -> None:
    def mutate(state: UpdateState) -> UpdateState:
        return replace(state, skip_until_version=version)

    _mutate_state(mutate, path=path)


def run_update_command(
    pending: PendingUpdate,
    *,
    subprocess_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return subprocess_run(
        pending.update_command,
        text=True,
        stdout=None,
        stderr=None,
        check=False,
    )


def check_for_updates_once(
    *,
    path: Path | None = None,
    current_version: str | None = None,
    release_date: str | None = None,
    http_client: Any | None = None,
    now: float | None = None,
    python_executable: str | None = None,
    python_version: str | None = None,
    force: bool = False,
) -> UpdateState:
    from iac_code import __release_date__, __version__

    state_path = path or get_update_state_path()
    state = load_update_state(state_path)
    checked_at = time.time() if now is None else now
    build_release_date = __release_date__ if release_date is None else release_date

    if not force and not build_release_date.strip():
        return state
    last_successful_check_at = state.last_successful_check_at
    if (
        not force
        and last_successful_check_at is not None
        and checked_at - last_successful_check_at < CHECK_THROTTLE_SECONDS
    ):
        return state

    installed_version = current_version or __version__
    python = python_executable or sys.executable
    runtime_python_version = python_version or _current_python_version()
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=10.0)
    try:
        official_version, official_success = _fetch_official_pypi_version(
            client,
            installed_version,
            runtime_python_version,
        )
        if official_version is not None:
            pending = PendingUpdate(
                version=official_version,
                current_version=installed_version,
                source=OFFICIAL_PYPI_SOURCE,
                checked_at=checked_at,
                update_command=_official_update_command(python),
                release_notes_url=_fetch_release_notes_url(client),
            )
            return _write_detected_state(
                state_path,
                pending,
                checked_at,
                source_success=True,
                current_version=installed_version,
                checked_sources={OFFICIAL_PYPI_SOURCE},
            )

        configured_version, configured_success = _fetch_configured_pip_version(python, installed_version)
        checked_sources = set()
        if official_success:
            checked_sources.add(OFFICIAL_PYPI_SOURCE)
        if configured_success:
            checked_sources.add(CONFIGURED_PIP_SOURCE)
        source_success = bool(checked_sources)
        pending = None
        if configured_version is not None:
            pending = PendingUpdate(
                version=configured_version,
                current_version=installed_version,
                source=CONFIGURED_PIP_SOURCE,
                checked_at=checked_at,
                update_command=_configured_update_command(python),
                release_notes_url=_fetch_release_notes_url(client),
            )
        return _write_detected_state(
            state_path,
            pending,
            checked_at,
            source_success=source_success,
            current_version=installed_version,
            checked_sources=checked_sources,
        )
    finally:
        if owns_client:
            client.close()


def start_background_update_check(
    *,
    path: Path | None = None,
    current_version: str | None = None,
    release_date: str | None = None,
    python_executable: str | None = None,
    check_func: Callable[..., UpdateState] = check_for_updates_once,
) -> threading.Thread:
    def run_check() -> None:
        try:
            check_func(
                path=path,
                current_version=current_version,
                release_date=release_date,
                python_executable=python_executable,
            )
        except Exception:
            _LOGGER.debug("Background update check failed", exc_info=True)

    thread = threading.Thread(target=run_check, name="iac-code-update-checker", daemon=True)
    thread.start()
    return thread


def _state_from_mapping(data: dict[Any, Any]) -> UpdateState:
    pending = _pending_from_mapping(data.get("pending"))
    last_successful_check_at = _optional_float(data.get("last_successful_check_at"))
    skip_until_version = data.get("skip_until_version")
    if skip_until_version is not None:
        skip_until_version = str(skip_until_version)

    return UpdateState(
        pending=pending,
        last_successful_check_at=last_successful_check_at,
        skip_until_version=skip_until_version,
    )


def _pending_from_mapping(data: Any) -> PendingUpdate | None:
    if not isinstance(data, dict):
        return None

    try:
        update_command = data["update_command"]
        if not isinstance(update_command, Iterable) or isinstance(update_command, (str, bytes)):
            return None
        return PendingUpdate(
            version=str(data["version"]),
            current_version=str(data["current_version"]),
            source=str(data["source"]),
            checked_at=float(data["checked_at"]),
            update_command=tuple(str(part) for part in update_command),
            release_notes_url=_optional_str(data.get("release_notes_url")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _version_or_none(value: str) -> Version | None:
    try:
        return Version(value)
    except InvalidVersion:
        return None


def _current_python_version() -> str:
    return "{}.{}.{}".format(sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def _latest_supported_version(versions: list[str], current_version: str) -> str | None:
    current = _version_or_none(current_version)
    if current is None:
        return None

    latest: tuple[Version, str] | None = None
    for raw_version in versions:
        parsed = _version_or_none(raw_version)
        if parsed is None:
            continue
        if parsed.is_prerelease and not current.is_prerelease:
            continue
        if parsed <= current:
            continue
        if latest is None or parsed > latest[0]:
            latest = (parsed, raw_version)
    return latest[1] if latest is not None else None


def _fetch_official_pypi_version(
    http_client: Any,
    current_version: str,
    python_version: str,
) -> tuple[str | None, bool]:
    try:
        response = http_client.get(_PYPI_JSON_URL)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _LOGGER.debug("Failed to fetch official PyPI versions", exc_info=True)
        return None, False

    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        return None, False
    installable_versions = [
        str(version) for version, files in releases.items() if _has_installable_pypi_release_file(files, python_version)
    ]
    return _latest_supported_version(installable_versions, current_version), True


def _has_installable_pypi_release_file(files: Any, python_version: str) -> bool:
    if not isinstance(files, list) or not files:
        return False
    return any(
        isinstance(file, dict)
        and not file.get("yanked")
        and _requires_python_allows(file.get("requires_python"), python_version)
        for file in files
    )


def _requires_python_allows(requires_python: Any, python_version: str) -> bool:
    if requires_python is None:
        return True
    specifier_text = str(requires_python).strip()
    if not specifier_text:
        return True
    try:
        return Version(python_version) in SpecifierSet(specifier_text)
    except (InvalidSpecifier, InvalidVersion):
        return False


def _parse_pip_index_versions(output: str, current_version: str) -> str | None:
    match = re.search(r"Available versions:\s*(?P<versions>.+)", output)
    if match is None:
        match = re.search(r"iac-code\s*\((?P<versions>[^)]+)\)", output)
    if match is None:
        return None

    versions = [version.strip() for version in match.group("versions").split(",") if version.strip()]
    return _latest_supported_version(versions, current_version)


def _fetch_configured_pip_version(python_executable: str, current_version: str) -> tuple[str | None, bool]:
    command = [
        python_executable,
        "-m",
        "pip",
        "index",
        "versions",
        "iac-code",
        "--disable-pip-version-check",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except Exception:
        _LOGGER.debug("Failed to run pip index versions for configured source", exc_info=True)
        return None, False

    if result.returncode != 0:
        return None, False
    return _parse_pip_index_versions(result.stdout, current_version), True


def _fetch_release_notes_url(http_client: Any) -> str:
    try:
        response = http_client.get(_GITHUB_LATEST_RELEASE_URL)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _LOGGER.debug("Failed to fetch latest GitHub release", exc_info=True)
        return DEFAULT_RELEASE_NOTES_URL

    if isinstance(payload, dict) and isinstance(payload.get("html_url"), str):
        return payload["html_url"]
    return DEFAULT_RELEASE_NOTES_URL


def _official_update_command(python_executable: str) -> tuple[str, ...]:
    return (
        python_executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--index-url",
        _PYPI_SIMPLE_INDEX_URL,
        "iac-code",
    )


def _configured_update_command(python_executable: str) -> tuple[str, ...]:
    return (python_executable, "-m", "pip", "install", "--upgrade", "iac-code")


def _write_detected_state(
    path: Path,
    pending: PendingUpdate | None,
    checked_at: float,
    *,
    source_success: bool,
    current_version: str | None = None,
    checked_sources: Iterable[str] = (),
) -> UpdateState:
    def mutate(state: UpdateState) -> UpdateState:
        checked_source_set = set(checked_sources)
        existing_pending = _normal_pending_for_merge(
            state.pending,
            current_version,
            checked_source_set,
            pending,
            checked_at,
        )
        pending_to_write = pending
        if existing_pending is not None:
            pending_to_write = (
                existing_pending
                if pending_to_write is None
                else _choose_pending_update(existing_pending, pending_to_write)
            )
        last_successful_check_at = state.last_successful_check_at
        if source_success:
            last_successful_check_at = max(state.last_successful_check_at or checked_at, checked_at)

        return UpdateState(
            pending=pending_to_write,
            last_successful_check_at=last_successful_check_at,
            skip_until_version=state.skip_until_version,
        )

    return _mutate_state(mutate, path=path)


def _normal_pending_for_merge(
    existing: PendingUpdate | None,
    current_version: str | None,
    checked_sources: set[str],
    detected: PendingUpdate | None,
    checked_at: float,
) -> PendingUpdate | None:
    if existing is None:
        return None

    if current_version is not None and not _is_newer_version(existing.version, current_version):
        return None

    detected_source = detected.source if detected is not None else None
    if existing.source in checked_sources and existing.source != detected_source:
        return existing if existing.checked_at > checked_at else None

    return existing


def _choose_pending_update(existing: PendingUpdate, detected: PendingUpdate) -> PendingUpdate:
    if _is_newer_version(existing.version, detected.version):
        return existing
    if _is_newer_version(detected.version, existing.version):
        return detected

    existing_source_rank = _source_rank(existing.source)
    detected_source_rank = _source_rank(detected.source)
    if existing_source_rank != detected_source_rank:
        return existing if existing_source_rank > detected_source_rank else detected

    return existing if existing.checked_at > detected.checked_at else detected


def _source_rank(source: str) -> int:
    if source == OFFICIAL_PYPI_SOURCE:
        return 2
    if source == CONFIGURED_PIP_SOURCE:
        return 1
    return 0


def _state_to_mapping(state: UpdateState) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if state.pending is not None:
        pending = asdict(state.pending)
        pending["update_command"] = list(state.pending.update_command)
        data["pending"] = pending
    if state.last_successful_check_at is not None:
        data["last_successful_check_at"] = state.last_successful_check_at
    if state.skip_until_version is not None:
        data["skip_until_version"] = state.skip_until_version
    return data


def _atomic_write_yaml(path: Path, state: UpdateState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            yaml.safe_dump(_state_to_mapping(state), file, sort_keys=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _advisory_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        lock_file = lock_path.open("a+", encoding="utf-8")
    except Exception:
        _LOGGER.debug("Failed to open update state advisory lock file", exc_info=True)
        yield
        return

    with lock_file:
        acquired = False
        if _fcntl is not None:
            try:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
                acquired = True
            except Exception:
                _LOGGER.debug("Failed to acquire update state advisory lock", exc_info=True)
        try:
            yield
        finally:
            if _fcntl is not None and acquired:
                try:
                    _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
                except Exception:
                    _LOGGER.debug("Failed to release update state advisory lock", exc_info=True)


def _mutate_state(mutator: Callable[[UpdateState], UpdateState], path: Path | None = None) -> UpdateState:
    state_path = path or get_update_state_path()
    with _STATE_LOCK:
        with _advisory_file_lock(state_path):
            state = mutator(load_update_state(state_path))
            _atomic_write_yaml(state_path, state)
            return state


def _is_newer_version(version: str, current_version: str) -> bool:
    try:
        return Version(version) > Version(current_version)
    except InvalidVersion:
        return False
