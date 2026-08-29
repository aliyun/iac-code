#!/usr/bin/env python3
"""Real end-to-end runner for the ``selling_solution_first`` pipeline.

The normal pytest suite imports this module to test the runner itself.  Real
providers, cloud APIs, PTYs, browsers, and native applications are touched only
after :func:`main` has validated the explicit E2E command-line opt-ins.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import importlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
PIPELINE_NAME = "selling_solution_first"
NEW_STEPS = (
    "solution_planning_and_selection",
    "materialize_selected_candidate",
    "deploying",
)
OLD_ONLY_STEPS = ("architecture_planning", "evaluate_candidates", "confirm_and_select")
CREDENTIAL_FILES = (".credentials.yml", ".cloud-credentials.yml")
DEFAULT_RUN_ROOT = Path(tempfile.gettempdir()) / "iac-code-selling-solution-first-e2e-runs"
DEFAULT_TEXT_MODEL = "deepseek-v4-flash-0731"
DEFAULT_MULTIMODAL_MODEL = "qwen3.8-max"
STACK_PREFIX = "iac-e2e-ssf"
WEB_E2E_PERMISSION_MODE = "bypass_permissions"


class Surface(str, Enum):
    A2A = "a2a"
    REPL = "repl"
    WEB = "web"
    DESKTOP = "desktop"
    LEGACY = "legacy"


@dataclass(frozen=True)
class ScenarioSpec:
    case_id: str
    name: str
    surface: Surface
    profile: str
    suites: frozenset[str]
    description: str
    cloud_write: bool = False
    multimodal: bool = False
    resource_lock: str = ""


def _spec(
    case_id: str,
    name: str,
    surface: Surface,
    profile: str,
    suites: str,
    description: str,
    *,
    cloud_write: bool = False,
    multimodal: bool = False,
    resource_lock: str = "",
) -> ScenarioSpec:
    return ScenarioSpec(
        case_id=case_id,
        name=name,
        surface=surface,
        profile=profile,
        suites=frozenset(suites.split()),
        description=description,
        cloud_write=cloud_write,
        multimodal=multimodal,
        resource_lock=resource_lock,
    )


# Keep this table in the same order as section 9-13 of the design document.
SCENARIOS: tuple[ScenarioSpec, ...] = (
    _spec(
        "A01",
        "a2a-happy-multi-plan",
        Surface.A2A,
        "happy_multi",
        "smoke core",
        "多方案选择、确认、部署与 handoff",
        cloud_write=True,
    ),
    _spec(
        "A02", "a2a-safe-quote-cancel", Surface.A2A, "safe_cancel", "core safety", "safe mode 询价、取消与零云写入"
    ),
    _spec("A03", "a2a-step1-clarify", Surface.A2A, "step1_clarify", "core", "Step 1 澄清后候选选择"),
    _spec("A04", "a2a-step1-replan-replace", Surface.A2A, "step1_replace", "core", "候选等待时修改并替换部署目标"),
    _spec("A05", "a2a-step2-required-parameter", Surface.A2A, "step2_parameter", "core", "Step 2 外部参数提问"),
    _spec(
        "A06",
        "a2a-step2-structured-override",
        Surface.A2A,
        "structured_override",
        "core",
        "结构化参数覆盖后重新预览与询价",
    ),
    _spec(
        "A07", "a2a-step2-reselect-new-intent", Surface.A2A, "reselect_new_intent", "core", "重新选择后再替换部署目标"
    ),
    _spec("A08", "a2a-non-aliyun-early-exit", Surface.A2A, "early_exit", "core", "非阿里云请求 early exit"),
    _spec(
        "A09",
        "a2a-performance-backup-restore",
        Surface.A2A,
        "backup_restore",
        "recovery",
        "四类 waiting state 的 backup restore",
    ),
    _spec(
        "A10", "a2a-input-during-backup", Surface.A2A, "input_during_backup", "recovery safety", "backup 窗口输入归类"
    ),
    _spec(
        "A11",
        "a2a-fault-checkpoints",
        Surface.A2A,
        "fault_checkpoints",
        "recovery safety",
        "关键持久化点 SIGKILL 恢复",
        cloud_write=True,
    ),
    _spec("A12", "a2a-running-step1", Surface.A2A, "running_step1", "recovery", "Step 1 running 恢复"),
    _spec("A13", "a2a-running-step2", Surface.A2A, "running_step2", "recovery", "Step 2 running 恢复"),
    _spec(
        "A14", "a2a-running-step3", Surface.A2A, "running_step3", "recovery", "Step 3 running 恢复", cloud_write=True
    ),
    _spec("A15", "a2a-normal-running", Surface.A2A, "normal_running", "recovery", "normal chat 流式恢复"),
    _spec("A16", "a2a-cancel-step1", Surface.A2A, "cancel_step1", "recovery", "取消 Step 1 running task"),
    _spec("A17", "a2a-cancel-step2", Surface.A2A, "cancel_step2", "recovery", "取消 Step 2 running task"),
    _spec(
        "A18",
        "a2a-cancel-step3",
        Surface.A2A,
        "cancel_step3",
        "recovery safety",
        "取消 Step 3 并受控清理",
        cloud_write=True,
    ),
    _spec(
        "A19",
        "a2a-rollback-recovery-step1",
        Surface.A2A,
        "rollback_step1",
        "recovery",
        "回滚后的 Step 1 恢复",
        cloud_write=True,
    ),
    _spec(
        "A20",
        "a2a-rollback-recovery-step2",
        Surface.A2A,
        "rollback_step2",
        "recovery",
        "回滚后的 Step 2 恢复",
        cloud_write=True,
    ),
    _spec(
        "A21",
        "a2a-rollback-recovery-step3",
        Surface.A2A,
        "rollback_step3",
        "recovery",
        "回滚后的 Step 3 恢复",
        cloud_write=True,
    ),
    _spec(
        "A22",
        "a2a-rollback-stack-cleanup",
        Surface.A2A,
        "rollback_cleanup",
        "recovery safety",
        "回滚 Stack 隔离清理",
        cloud_write=True,
        resource_lock="rollback-stack-cleanup",
    ),
    _spec(
        "A23",
        "a2a-rollback-cleanup-recovery",
        Surface.A2A,
        "rollback_cleanup_recovery",
        "recovery safety",
        "cleanup 中恢复",
        cloud_write=True,
        resource_lock="rollback-stack-cleanup",
    ),
    _spec("A24", "a2a-redaction-contract", Surface.A2A, "redaction", "safety", "公开载荷、价格和凭证脱敏契约"),
    _spec(
        "A25",
        "a2a-image-initial-selection",
        Surface.A2A,
        "image_initial",
        "multimodal",
        "图片启动和选择",
        multimodal=True,
    ),
    _spec(
        "A26",
        "a2a-image-asks-confirmation",
        Surface.A2A,
        "image_asks",
        "multimodal",
        "图片回答 ask 和调整参数",
        multimodal=True,
    ),
    _spec(
        "A27",
        "a2a-image-interrupt-handoff",
        Surface.A2A,
        "image_interrupt",
        "multimodal",
        "图片回滚和 handoff",
        cloud_write=True,
        multimodal=True,
    ),
    _spec(
        "R01",
        "repl-single-plan-happy",
        Surface.REPL,
        "happy_single",
        "smoke core",
        "单候选 UI、确认、部署和 normal chat",
        cloud_write=True,
    ),
    _spec(
        "R02",
        "repl-multi-plan-natural-adjust",
        Surface.REPL,
        "natural_adjust",
        "core",
        "方向键选择和直接输入调参",
        cloud_write=True,
    ),
    _spec("R03", "repl-step1-clarify-replan", Surface.REPL, "step1_clarify", "core", "Step 1 自由输入和重规划"),
    _spec("R04", "repl-step1-replace-invalid-select", Surface.REPL, "replace_invalid", "core", "无效选择后替换 intent"),
    _spec("R05", "repl-step2-required-parameter", Surface.REPL, "step2_parameter", "core", "Step 2 外部参数输入"),
    _spec("R06", "repl-step2-reselect-progress", Surface.REPL, "reselect_progress", "core", "reselect 后进度条重置"),
    _spec(
        "R07", "repl-waiting-resume-all", Surface.REPL, "waiting_resume", "recovery", "四类 waiting state 退出和恢复"
    ),
    _spec("R08", "repl-running-step1", Surface.REPL, "running_step1", "recovery", "Step 1 thinking 中恢复"),
    _spec("R09", "repl-running-step2", Surface.REPL, "running_step2", "recovery", "Step 2 工具流中恢复"),
    _spec(
        "R10", "repl-running-step3", Surface.REPL, "running_step3", "recovery", "Step 3 创建中恢复", cloud_write=True
    ),
    _spec(
        "R11",
        "repl-normal-running-cancel-resume",
        Surface.REPL,
        "normal_resume",
        "recovery",
        "normal response Ctrl+C 后继续",
    ),
    _spec(
        "R12",
        "repl-interrupt-rollback",
        Surface.REPL,
        "interrupt_rollback",
        "recovery",
        "Step 2/3 两次 interrupt 回滚",
        cloud_write=True,
    ),
    _spec(
        "R13",
        "repl-rollback-cleanup-recovery",
        Surface.REPL,
        "cleanup_recovery",
        "recovery",
        "cleanup 中 REPL 恢复",
        cloud_write=True,
        resource_lock="rollback-stack-cleanup",
    ),
    _spec(
        "R14",
        "repl-multimodal-lifecycle",
        Surface.REPL,
        "multimodal",
        "multimodal",
        "完整图片生命周期",
        multimodal=True,
    ),
    _spec(
        "W01",
        "web-full-flow",
        Surface.WEB,
        "full_flow",
        "smoke web",
        "真实浏览器完整流程",
        cloud_write=True,
        resource_lock="browser",
    ),
    _spec(
        "W02",
        "web-multimodal-cancel-recovery",
        Surface.WEB,
        "multimodal_cancel",
        "multimodal web",
        "Web 图片、刷新、回滚和取消",
        multimodal=True,
        resource_lock="browser",
    ),
    _spec(
        "D01",
        "desktop-native-full-flow",
        Surface.DESKTOP,
        "native_full",
        "safety desktop",
        "原生 Desktop host 与 sidecar 完整流程",
        resource_lock="desktop-native",
    ),
    _spec("L01", "legacy-selling-smoke", Surface.LEGACY, "legacy_smoke", "safety legacy", "原 selling 五步兼容冒烟"),
)

SCENARIO_BY_NAME = {item.name: item for item in SCENARIOS}
SUITE_NAMES = ("smoke", "core", "recovery", "multimodal", "safety", "web", "desktop", "legacy", "all")


def scenarios_for_suite(name: str) -> list[ScenarioSpec]:
    if name == "all":
        return list(SCENARIOS)
    if name not in SUITE_NAMES:
        raise ValueError(f"unknown suite: {name}")
    return [item for item in SCENARIOS if name in item.suites]


def select_scenarios(names: Sequence[str], suites: Sequence[str]) -> list[ScenarioSpec]:
    selected = set(names)
    default_suites = () if names else ("smoke",)
    for suite in suites or default_suites:
        selected.update(item.name for item in scenarios_for_suite(suite))
    unknown = selected.difference(SCENARIO_BY_NAME)
    if unknown:
        raise ValueError("unknown scenario(s): " + ", ".join(sorted(unknown)))
    return [item for item in SCENARIOS if item.name in selected]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real selling_solution_first E2E scenarios.")
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--suite", action="append", choices=SUITE_NAMES, default=[])
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--concurrency", type=positive_int, default=3)
    parser.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--run-dir", default="")
    parser.add_argument("--credential-source-dir", default="~/.iac-code")
    parser.add_argument("--inherit-settings", action="store_true")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-base", default="")
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--allow-cloud-write", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--skip-final-teardown", action="store_true")
    parser.add_argument("--leave-running", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--stream-timeout", type=float, default=1800.0)
    parser.add_argument("--preflight-timeout", type=float, default=90.0)
    parser.add_argument("--terminal-width", type=int, default=160)
    parser.add_argument("--terminal-height", type=int, default=48)
    parser.add_argument("--desktop-command", default="")
    parser.add_argument("--desktop-package-root", default=str(REPO_ROOT / "desktop" / "dist"))
    parser.add_argument("--cleanup-vpc-id", default="")
    parser.add_argument("--cleanup-vpc-cidr", default="")
    parser.add_argument("--cleanup-zone-id", default="")
    parser.add_argument("--occupied-cidr", action="append", default=[])
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace, selected: Sequence[ScenarioSpec]) -> None:
    if args.run_dir and (len(selected) != 1 or args.concurrency != 1):
        raise ValueError("--run-dir requires exactly one scenario and --concurrency 1")
    if args.leave_running and (len(selected) != 1 or args.concurrency != 1):
        raise ValueError("--leave-running requires exactly one scenario and --concurrency 1")
    if not args.allow_real_cloud:
        raise ValueError("real E2E requires --allow-real-cloud")
    writers = [item.name for item in selected if item.cloud_write]
    if writers and not args.allow_cloud_write:
        raise ValueError("cloud-write scenario(s) require --allow-cloud-write: " + ", ".join(writers))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any, lock: threading.Lock | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    context = lock if lock is not None else contextlib.nullcontext()
    with context, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class CredentialMetadata:
    exists: bool
    size: int = 0
    mtime_ns: int = 0
    digest: str = ""


def snapshot_credentials(source_dir: Path) -> dict[str, CredentialMetadata]:
    result: dict[str, CredentialMetadata] = {}
    for name in CREDENTIAL_FILES:
        path = source_dir / name
        if not path.is_file():
            result[name] = CredentialMetadata(exists=False)
            continue
        info = path.stat()
        result[name] = CredentialMetadata(True, info.st_size, info.st_mtime_ns, sha256_file(path))
    return result


def credential_snapshot_unchanged(
    before: Mapping[str, CredentialMetadata], after: Mapping[str, CredentialMetadata]
) -> bool:
    return dict(before) == dict(after)


@dataclass(frozen=True)
class CredentialCopyAudit:
    credential_files_copied: bool
    settings_copied: bool
    directory_mode_ok: bool
    file_modes_ok: bool
    independent_files: bool
    missing: tuple[str, ...]


def copy_credentials(source_dir: Path, destination: Path, *, inherit_settings: bool) -> CredentialCopyAudit:
    from iac_code.utils.file_security import ensure_private_dir, ensure_private_file

    source_dir = source_dir.expanduser().resolve()
    ensure_private_dir(destination)
    missing: list[str] = []
    copied: list[tuple[Path, Path]] = []
    for name in CREDENTIAL_FILES:
        source = source_dir / name
        target = destination / name
        if not source.is_file():
            missing.append(name)
            continue
        if source.is_symlink():
            raise ValueError(f"credential source must be a regular non-symlink file: {source}")
        shutil.copyfile(source, target, follow_symlinks=False)
        ensure_private_file(target)
        copied.append((source, target))
    settings_copied = False
    if inherit_settings:
        source = source_dir / "settings.yml"
        if source.is_file():
            if source.is_symlink():
                raise ValueError(f"settings source must be a regular non-symlink file: {source}")
            target = destination / "settings.yml"
            shutil.copyfile(source, target, follow_symlinks=False)
            ensure_private_file(target)
            copied.append((source, target))
            settings_copied = True
    directory_mode_ok = os.name == "nt" or stat.S_IMODE(destination.stat().st_mode) == 0o700
    file_modes_ok = os.name == "nt" or all(stat.S_IMODE(target.stat().st_mode) == 0o600 for _, target in copied)
    independent_files = all(
        not target.is_symlink() and not os.path.samefile(source, target) for source, target in copied
    )
    return CredentialCopyAudit(
        credential_files_copied=not missing,
        settings_copied=settings_copied,
        directory_mode_ok=directory_mode_ok,
        file_modes_ok=file_modes_ok,
        independent_files=independent_files,
        missing=tuple(missing),
    )


def read_runtime_defaults(source_dir: Path) -> dict[str, str]:
    settings_path = source_dir.expanduser().resolve() / "settings.yml"
    if not settings_path.is_file():
        return {}
    try:
        value = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    # Read only the same non-secret selectors used by iac_code.config. The source
    # settings file is never used as a case runtime file unless --inherit-settings.
    provider = value.get("activeProvider") or value.get("provider") or value.get("default_provider")
    provider_entry: Mapping[str, Any] = {}
    providers = value.get("providers")
    if isinstance(provider, str) and isinstance(providers, dict):
        entry = providers.get(provider)
        if isinstance(entry, dict):
            provider_entry = entry
    model = provider_entry.get("model") or value.get("model") or value.get("default_model")
    api_base = (
        provider_entry.get("apiBase")
        or provider_entry.get("api_base")
        or value.get("api_base")
        or value.get("base_url")
    )
    return {
        key: str(item)
        for key, item in (("provider", provider), ("model", model), ("api_base", api_base))
        if isinstance(item, str) and item
    }


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class RuntimePaths:
    run_dir: Path
    config_dir: Path
    backup_dir: Path
    workspace_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    templates_dir: Path
    snapshots_dir: Path

    @classmethod
    def create(cls, run_dir: Path, credential_source_dir: Path) -> RuntimePaths:
        resolved = run_dir.expanduser().resolve()
        values = cls(
            run_dir=resolved,
            config_dir=resolved / "config",
            backup_dir=resolved / "config-backup",
            workspace_dir=resolved / "workspace",
            artifacts_dir=resolved / "artifacts",
            logs_dir=resolved / "logs",
            templates_dir=resolved / "templates",
            snapshots_dir=resolved / "pipeline-snapshots",
        )
        values.validate(credential_source_dir)
        for path in dataclasses.astuple(values)[1:]:
            Path(path).mkdir(parents=True, exist_ok=True)
        return values

    def validate(self, credential_source_dir: Path) -> None:
        source = credential_source_dir.expanduser().resolve()
        children = (self.config_dir, self.backup_dir, self.workspace_dir, self.artifacts_dir, self.logs_dir)
        if any(not is_relative_to(path.resolve(), self.run_dir) for path in children):
            raise ValueError("every runtime path must be inside its case run directory")
        if len({path.resolve() for path in children}) != len(children):
            raise ValueError("runtime paths must be distinct")
        if (
            self.config_dir.resolve() == source
            or is_relative_to(source, self.config_dir.resolve())
            or is_relative_to(self.config_dir.resolve(), source)
        ):
            raise ValueError("case config must not contain the credential source directory")
        if (
            self.backup_dir.resolve() == source
            or is_relative_to(source, self.backup_dir.resolve())
            or is_relative_to(self.backup_dir.resolve(), source)
        ):
            raise ValueError("case backup must not contain the credential source directory")


class PortAllocator:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self._lock = threading.Lock()
        self._reserved: set[int] = set()

    def reserve(self) -> int:
        with self._lock:
            while True:
                with socket.socket() as sock:
                    sock.bind((self.host, 0))
                    port = int(sock.getsockname()[1])
                if port not in self._reserved:
                    self._reserved.add(port)
                    return port


class CidrAllocator:
    def __init__(self, occupied: Iterable[str] = (), pool_cidr: str = "10.250.0.0/16") -> None:
        self._lock = threading.Lock()
        self._occupied: set[ipaddress.IPv4Network] = {ipaddress.IPv4Network(value) for value in occupied}
        self._reserved: set[ipaddress.IPv4Network] = set()
        self._pool = ipaddress.IPv4Network(pool_cidr, strict=False)

    def reserve(self) -> str:
        with self._lock:
            prefix = max(24, self._pool.prefixlen)
            candidates = (self._pool,) if prefix == self._pool.prefixlen else self._pool.subnets(new_prefix=prefix)
            for candidate in candidates:
                if any(candidate.overlaps(network) for network in self._occupied | self._reserved):
                    continue
                self._reserved.add(candidate)
                return str(candidate)
        raise RuntimeError("the selling_solution_first E2E CIDR pool is exhausted")


class ResourceLockManager:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextlib.contextmanager
    def acquire(self, name: str) -> Iterator[None]:
        if not name:
            yield
            return
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            yield


@dataclass
class ScenarioResult:
    case_id: str
    scenario: str
    surface: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    run_dir: str
    checks: dict[str, bool]
    notes: list[str]
    cleanup_status: str
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed" and all(self.checks.values())


@dataclass
class ScenarioRuntime:
    spec: ScenarioSpec
    args: argparse.Namespace
    paths: RuntimePaths
    port: int
    cidr: str
    stack_name: str
    env: dict[str, str]
    credential_audit: CredentialCopyAudit
    cancel_event: threading.Event
    event_lock: threading.Lock
    processes: list[subprocess.Popen[Any]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    cloud_resources: list[dict[str, Any]] = field(default_factory=list)
    owned_stack_names: set[str] = field(default_factory=set)
    repl_candidate_wait_count: int = 0
    repl_confirmation_wait_count: int = 0
    repl_confirmation_action_count: int = 0

    @property
    def events_path(self) -> Path:
        return self.paths.run_dir / "events.jsonl"

    def event(self, event_type: str, **data: Any) -> None:
        append_jsonl(
            self.events_path,
            {"at": utc_now(), "caseId": self.spec.case_id, "scenario": self.spec.name, "type": event_type, **data},
            self.event_lock,
        )

    def register_process(self, process: subprocess.Popen[Any]) -> None:
        if process not in self.processes:
            self.processes.append(process)

    @staticmethod
    def _signal_process(process: subprocess.Popen[Any], signal_number: int) -> None:
        if os.name == "nt":
            process.terminate()
            return
        with contextlib.suppress(OSError):
            process_group = os.getpgid(process.pid)
            if process_group == process.pid:
                os.killpg(process_group, signal_number)
                return
        process.send_signal(signal_number)

    def terminate_processes(self) -> bool:
        clean = True
        for process in reversed(self.processes):
            if process.poll() is not None:
                continue
            try:
                self._signal_process(process, signal.SIGINT)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                clean = False
                with contextlib.suppress(OSError):
                    if os.name == "nt":
                        process.kill()
                    else:
                        self._signal_process(process, signal.SIGKILL)
                    process.wait(timeout=5)
        return clean and all(process.poll() is not None for process in self.processes)


@dataclass
class RunnerServices:
    ports: PortAllocator = field(default_factory=PortAllocator)
    cidrs: CidrAllocator = field(default_factory=CidrAllocator)
    locks: ResourceLockManager = field(default_factory=ResourceLockManager)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    suite_event_lock: threading.Lock = field(default_factory=threading.Lock)
    runtime_lock: threading.Lock = field(default_factory=threading.Lock)
    active_runtimes: dict[str, ScenarioRuntime] = field(default_factory=dict)

    def register_runtime(self, runtime: ScenarioRuntime) -> None:
        with self.runtime_lock:
            self.active_runtimes[runtime.spec.name] = runtime

    def unregister_runtime(self, runtime: ScenarioRuntime) -> None:
        with self.runtime_lock:
            self.active_runtimes.pop(runtime.spec.name, None)

    def terminate_active_processes(self) -> bool:
        with self.runtime_lock:
            runtimes = list(self.active_runtimes.values())
        clean = True
        for runtime in runtimes:
            if not runtime.terminate_processes():
                clean = False
        return clean


def case_run_dir(root: Path, spec: ScenarioSpec, explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    token = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return root.expanduser().resolve() / spec.name / token


def create_runtime(
    spec: ScenarioSpec,
    args: argparse.Namespace,
    services: RunnerServices,
    runtime_defaults: Mapping[str, str],
) -> ScenarioRuntime:
    source = Path(args.credential_source_dir).expanduser().resolve()
    paths = RuntimePaths.create(case_run_dir(Path(args.run_root), spec, args.run_dir), source)
    audit = copy_credentials(source, paths.config_dir, inherit_settings=args.inherit_settings)
    if not audit.credential_files_copied:
        raise FileNotFoundError("missing credential file(s) in source directory: " + ", ".join(audit.missing))
    port = services.ports.reserve()
    cidr = services.cidrs.reserve()
    suffix = uuid.uuid4().hex[:8]
    compact_name = re.sub(r"[^a-z0-9]+", "-", spec.name.lower()).strip("-")[:38]
    stack_name = f"{STACK_PREFIX}-{compact_name}-{suffix}"[:64]
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "IAC_CODE_MODE": "pipeline",
            "IAC_CODE_PIPELINE_NAME": "selling" if spec.surface is Surface.LEGACY else PIPELINE_NAME,
            "IAC_CODE_CONFIG_DIR": str(paths.config_dir),
            "IAC_CODE_CONFIG_BACKUP_DIR": str(paths.backup_dir),
            "IAC_CODE_E2E_WORKSPACE": str(paths.workspace_dir),
            "IAC_CODE_E2E_STACK_NAME": stack_name,
            "IAC_CODE_E2E_RESERVED_CIDR": cidr,
        }
    )
    provider = args.provider or runtime_defaults.get("provider", "")
    model = (
        args.model
        or (DEFAULT_MULTIMODAL_MODEL if spec.multimodal else runtime_defaults.get("model", ""))
        or DEFAULT_TEXT_MODEL
    )
    api_base = args.api_base or runtime_defaults.get("api_base", "")
    if provider:
        env["IAC_CODE_PROVIDER"] = provider
    if model:
        env["IAC_CODE_MODEL"] = model
    if api_base:
        env["IAC_CODE_BASE_URL"] = api_base
    if spec.profile == "safe_cancel":
        env["IAC_CODE_A2A_SAFE_MODE"] = "true"
    if spec.profile in {"backup_restore", "input_during_backup"}:
        env["IAC_CODE_A2A_EXTREME_PERFORMANCE"] = "true"
    runtime = ScenarioRuntime(
        spec=spec,
        args=args,
        paths=paths,
        port=port,
        cidr=cidr,
        stack_name=stack_name,
        env=env,
        credential_audit=audit,
        cancel_event=services.cancel_event,
        event_lock=threading.Lock(),
        owned_stack_names={stack_name},
    )
    runtime.checks.update(
        {
            "config isolated": is_relative_to(paths.config_dir, paths.run_dir),
            "backup isolated": is_relative_to(paths.backup_dir, paths.run_dir),
            "workspace isolated": is_relative_to(paths.workspace_dir, paths.run_dir),
            "credential files copied": audit.credential_files_copied,
            "credential permissions": audit.directory_mode_ok and audit.file_modes_ok,
            "credential copies independent": audit.independent_files,
            "unique port assigned": port > 0,
            "unique cloud identity assigned": stack_name.startswith(STACK_PREFIX + "-") and bool(cidr),
        }
    )
    write_json(paths.run_dir / "config-audit.json", _credential_audit_payload(runtime))
    write_json(paths.run_dir / "cloud-resources.json", [])
    return runtime


def _credential_audit_payload(runtime: ScenarioRuntime) -> dict[str, Any]:
    audit = runtime.credential_audit
    return {
        "credentialFilesCopied": audit.credential_files_copied,
        "settingsCopied": audit.settings_copied,
        "directoryModeOk": audit.directory_mode_ok,
        "fileModesOk": audit.file_modes_ok,
        "independentFiles": audit.independent_files,
        "missingCredentialFileNames": list(audit.missing),
        "configIsolated": is_relative_to(runtime.paths.config_dir, runtime.paths.run_dir),
        "backupIsolated": is_relative_to(runtime.paths.backup_dir, runtime.paths.run_dir),
        "workspaceIsolated": is_relative_to(runtime.paths.workspace_dir, runtime.paths.run_dir),
    }


def _legacy_a2a_module() -> Any:
    return importlib.import_module("scripts.a2a.e2e.run_recovery_scenarios")


def _legacy_repl_module() -> Any:
    return importlib.import_module("scripts.repl.e2e.run_pipeline_scenarios")


def _web_module() -> Any:
    return importlib.import_module("scripts.web.e2e.run_contract_scenario")


def _track_a2a_server_processes(runtime: ScenarioRuntime, harness: Any) -> None:
    start_server = harness.start_server

    def tracked_start_server() -> None:
        start_server()
        process = getattr(getattr(harness, "server", None), "process", None)
        if isinstance(process, subprocess.Popen):
            runtime.register_process(process)

    harness.start_server = tracked_start_server


def _python_namespace(runtime: ScenarioRuntime) -> argparse.Namespace:
    args = runtime.args
    return argparse.Namespace(
        scenario=[],
        host="127.0.0.1",
        port=runtime.port,
        cwd=str(runtime.paths.workspace_dir),
        server_cwd=str(REPO_ROOT),
        run_root=str(runtime.paths.run_dir.parent),
        run_dir=str(runtime.paths.run_dir),
        python=args.python,
        provider=runtime.env.get("IAC_CODE_PROVIDER", ""),
        model=runtime.env.get("IAC_CODE_MODEL", ""),
        api_base=runtime.env.get("IAC_CODE_BASE_URL", ""),
        deterministic=False,
        fault_at="",
        allow_real_cloud=True,
        skip_preflight=True,
        preflight_timeout=args.preflight_timeout,
        server_timeout=args.timeout,
        stream_timeout=args.stream_timeout,
        event_timeout=args.timeout,
        leave_server_running=args.leave_running,
        no_auto_approve_permissions=False,
        initial_prompt="",
        selection_prompt="",
        normal_followup_prompt="你刚才完成了什么？只依据本会话回答。",
        recovery_prompt="继续恢复未完成的流程。",
        expected_text="",
        redaction_step4_prompt="",
        timeout=args.timeout,
        terminal_width=args.terminal_width,
        terminal_height=args.terminal_height,
        candidate_selection_ready_timeout=args.timeout,
        leave_running=args.leave_running,
        skip_final_teardown=args.skip_final_teardown,
        final_teardown_timeout=args.stream_timeout,
        cleanup_vpc_id=args.cleanup_vpc_id,
        cleanup_vpc_cidr=args.cleanup_vpc_cidr,
        cleanup_zone_id=args.cleanup_zone_id,
        cleanup_vswitch_cidr=runtime.cidr,
        cleanup_rollback_vswitch_cidr="",
        permission_prompt_response="pageup-enter",
        ask_prompt="",
        ask_answer="",
        normal_followup_prompt_repl="你刚才完成了什么？",
        rollback_prompt="",
        invalid_selection_prompt="9",
        evaluate_resume_continue_prompt="继续",
        cleanup_continue_prompt="继续恢复测试自有资源的清理。",
    )


def _initial_prompt(runtime: ScenarioRuntime) -> str:
    spec = runtime.spec
    stack = runtime.stack_name
    cidr = runtime.cidr
    base = (
        "请在阿里云杭州地域为一个测试应用设计并部署网络基础设施。至少给出两个详细架构方案，"
        "说明架构图、资源清单、价格概览和费用明细。最终 ROS StackName 必须使用 "
        f"{stack}，如需 VSwitch 使用 runner 预留网段 {cidr}。"
    )
    prompts = {
        "happy_multi": base,
        "safe_cancel": base + "本轮只完成模板、Preview 和询价，不创建资源。",
        "step1_clarify": "我有个产品要上线。",
        "step1_replace": base + "先提供两个网络方案，等待我修改。",
        "step2_parameter": (
            "只设计一个复用已有 VPC 创建 VSwitch 的方案。我还没有提供真实 VpcId 和 ZoneId；"
            "这两个值都是 user_required 外部参数，禁止通过 API、默认值或推断自行选择。"
            "进入方案实现阶段后必须用 ask_user_question 逐项向我确认 VpcId 和 ZoneId，"
            f"拿到两个回答后才能 Preview 和询价。预留 VSwitch 网段为 {cidr}，本轮不部署。"
        ),
        "structured_override": base + "候选选择后允许我覆盖 VSwitch 网段，本轮不部署。",
        "reselect_new_intent": base + "必须给出两个可独立选择的方案，本轮不部署。",
        "early_exit": "请为 AWS 账号创建一个 Amazon VPC，不使用阿里云，也不生成 ROS 模板。",
        "backup_restore": ("我有个产品要上线；请先向我澄清需求。后续方案必须复用已有 VPC，并在实现阶段询问 VPC ID。"),
        "input_during_backup": (
            "我有个产品要上线；请先向我澄清需求。后续方案必须复用已有 VPC，并在实现阶段询问 VPC ID。"
        ),
        "waiting_resume": ("我有个产品要上线；请先向我澄清需求。后续方案必须复用已有 VPC，并在实现阶段询问 VPC ID。"),
        "redaction": (
            "在阿里云创建一个收费数据库测试方案，模板包含 NoEcho 管理员密码参数。展示真实询价数字和"
            "必要模板参数但绝不展示凭证；只到部署确认，不创建资源。"
        ),
        "image_initial": base + "本轮不部署。",
        "image_asks": "我有个产品要上线；需要通过问题澄清，并在实现阶段询问必要参数。本轮不部署。",
        "image_interrupt": base,
        "legacy_smoke": "在已有 VPC 中创建一个 VSwitch，给出多个候选，本轮不部署。",
    }
    if spec.profile.startswith("rollback"):
        return base + "稍后我会改变部署目标，用于验证回滚恢复。"
    if (
        spec.profile.startswith("running")
        or spec.profile.startswith("cancel")
        or spec.profile in {"fault_checkpoints", "normal_running"}
    ):
        return base
    return prompts.get(spec.profile, base)


def _candidate_payload(index: int = 0, *, with_ignored_override: bool = False) -> str:
    payload: dict[str, Any] = {
        "selected_candidate_index": index,
        "selected_evaluated_candidate_index": index,
    }
    if with_ignored_override:
        payload["parameter_overrides"] = {"CidrBlock": "10.99.99.0/24"}
    return json.dumps(payload, ensure_ascii=False)


def _confirmation_payload(action: str, overrides: Mapping[str, Any] | None = None) -> str:
    return json.dumps({"action": action, "parameter_overrides": dict(overrides or {})}, ensure_ascii=False)


def _event_files(run_dir: Path) -> list[Path]:
    paths = [path for path in run_dir.glob("*.events.jsonl") if path.name != "events.jsonl"]
    request_order: dict[str, int] = {}
    for index, request in enumerate(_read_json_lines(run_dir / "requests.jsonl")):
        if isinstance(request, dict) and isinstance(request.get("name"), str):
            request_order.setdefault(request["name"], index)
    return sorted(
        paths,
        key=lambda path: (
            request_order.get(path.name.removesuffix(".events.jsonl"), len(request_order)),
            path.name,
        ),
    )


def _read_json_lines(path: Path) -> list[Any]:
    values: list[Any] = []
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def _all_event_values(run_dir: Path) -> list[Any]:
    values: list[Any] = []
    for path in _event_files(run_dir):
        values.extend(_read_json_lines(path))
    return values


def _walk(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            # Array elements have no mapping key of their own, but callers that
            # inspect nested event dictionaries still need to see the element.
            yield "", item
            yield from _walk(item)


def _json_text(values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, default=str)


def _tool_sequence(values: Sequence[Any]) -> list[dict[str, Any]]:
    sequence: list[dict[str, Any]] = []
    tool_keys = {"toolName", "tool_name", "name"}
    for event_index, value in enumerate(values):
        for key, item in _walk(value):
            if key not in tool_keys or not isinstance(item, str):
                continue
            lowered = item.lower()
            if any(marker in lowered for marker in ("aliyun_api", "ros_deploy", "write", "edit", "bash")):
                sequence.append({"index": len(sequence), "eventIndex": event_index, "tool": item})
    return sequence


def _started_steps(values: Sequence[Any]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for event_index, value in enumerate(values):
        candidates = [item for _, item in _walk(value) if isinstance(item, dict)]
        if isinstance(value, dict):
            candidates.append(value)
        for item in candidates:
            event_type = item.get("eventType") or item.get("event_type") or item.get("type")
            if event_type != "step_started":
                continue
            step = item.get("step")
            step_id = step.get("id") if isinstance(step, dict) else item.get("step_id")
            if isinstance(step_id, str):
                pair = (event_index, step_id)
                if pair not in result:
                    result.append(pair)
    return result


def _has_unhandled_terminal_error(value: Any) -> bool:
    """Detect runner/transport terminal errors without treating handled tool failures as fatal."""
    if isinstance(value, dict):
        event_type = value.get("eventType") or value.get("event_type")
        if event_type == "tool_result":
            return False
        # A REPL transcript is a flattened terminal rendering and therefore also
        # contains handled tool stdout/stderr. PTY/expect failures are recorded as
        # separate structured events, so inspect those instead of treating a tool's
        # traceback text as a crash of the REPL itself.
        return any(_has_unhandled_terminal_error(item) for key, item in value.items() if key != "transcript")
    if isinstance(value, list):
        return any(_has_unhandled_terminal_error(item) for item in value)
    if not isinstance(value, str):
        return False
    return any(marker in value for marker in ("Traceback (most recent call last)", "pexpect.TIMEOUT", "pexpect.EOF"))


def _common_pipeline_checks(runtime: ScenarioRuntime, values: Sequence[Any]) -> None:
    text = _json_text(values)
    sequence = _tool_sequence(values)
    write_json(runtime.paths.run_dir / "tool-sequence.json", sequence)
    runtime.checks["pipeline name is correct"] = runtime.env["IAC_CODE_PIPELINE_NAME"] == (
        "selling" if runtime.spec.surface is Surface.LEGACY else PIPELINE_NAME
    )
    if runtime.spec.surface in {Surface.A2A, Surface.LEGACY}:
        observed_input_required = any(
            key in {"eventType", "event_type"} and item == "input_required"
            for value in values
            for key, item in _walk(value)
        )
        if runtime.checks.get("A2A waiting input was exercised") or observed_input_required:
            runtime.checks["A2A waiting input was exercised"] = True
    runtime.checks["no unhandled terminal error"] = not any(_has_unhandled_terminal_error(value) for value in values)
    if runtime.spec.surface is Surface.LEGACY:
        runtime.checks["legacy pipeline not rewritten"] = "selling_solution_first" not in text
        return
    runtime.checks["old step ids absent"] = not any(step in text for step in OLD_ONLY_STEPS)
    started_steps = _started_steps(values)
    first_positions = [
        next((event_index for event_index, observed in started_steps if observed == step), -1) for step in NEW_STEPS
    ]
    observed_positions = [position for position in first_positions if position >= 0]
    runtime.checks["new step order preserved"] = observed_positions == sorted(observed_positions)
    runtime.checks["candidate sub-pipeline absent"] = "candidate_step_started" not in text
    event_texts = [_json_text(value) for value in values]
    step2_index = next(
        (event_index for event_index, step in started_steps if step == NEW_STEPS[1]),
        len(event_texts),
    )
    step1_text = "".join(event_texts[:step2_index])
    runtime.checks["Step 1 has no materialization or exact quote"] = not any(
        marker in step1_text
        for marker in ("PreviewStack", "GetTemplateEstimateCost", '"toolName": "write"', '"tool_name": "write"')
    )
    ros_event_indexes = [item["eventIndex"] for item in sequence if item["tool"].lower() == "ros_deploy"]
    confirmation_indexes: list[int] = []
    repl_unstructured_confirmation_indexes: list[int] = []
    for event_index, value in enumerate(values):
        candidates = [item for _, item in _walk(value) if isinstance(item, dict)]
        if isinstance(value, dict):
            candidates.append(value)
        for item in candidates:
            event_type = item.get("eventType") or item.get("event_type") or item.get("type")
            payload = item.get("data") or item.get("payload")
            if (
                event_type not in {"input_received", "user_input_received"}
                or not isinstance(payload, dict)
                or payload.get("kind") != "deployment_confirmation"
            ):
                continue
            if payload.get("action") == "confirm":
                confirmation_indexes.append(event_index)
            elif runtime.spec.surface is Surface.REPL and payload.get("structured") is False:
                repl_unstructured_confirmation_indexes.append(event_index)
    if runtime.spec.surface is Surface.REPL:
        step3_indexes = [event_index for event_index, step in started_steps if step == NEW_STEPS[2]]
        if step3_indexes:
            classified_before_step3 = [
                event_index
                for event_index in repl_unstructured_confirmation_indexes
                if event_index < min(step3_indexes)
            ]
            if classified_before_step3:
                # For free text, the runner deliberately does not invent an
                # action field. Entering Step 3 proves the immediately preceding
                # Step 2 answer was classified as confirmation by the LLM.
                confirmation_indexes.append(max(classified_before_step3))
    runtime.checks["no deploy before confirmation"] = not ros_event_indexes or (
        bool(confirmation_indexes) and min(ros_event_indexes) > min(confirmation_indexes)
    )
    if runtime.spec.profile == "safe_cancel":
        runtime.checks["cancel kept the deployment unattempted"] = not ros_event_indexes
        runtime.checks["safe mode and cancel made no cloud write"] = not discover_cloud_resources(runtime)
    elif runtime.spec.profile == "early_exit":
        runtime.checks["early exit made no cloud write"] = not ros_event_indexes
    confirmation_payloads = [
        item.get("data")
        for _, item in _walk(values)
        if isinstance(item, dict)
        and (item.get("eventType") == "input_required" or item.get("event_type") == "input_required")
        and isinstance(item.get("data"), dict)
        and item["data"].get("kind") == "deployment_confirmation"
    ]
    if confirmation_payloads:
        runtime.checks["confirmation includes current solution and quote"] = any(
            isinstance(payload, dict)
            and bool(str(payload.get("solution_summary") or payload.get("solutionSummary") or "").strip())
            and isinstance(payload.get("cost"), dict)
            and bool(str(payload["cost"].get("monthly_estimate") or "").strip())
            and isinstance(payload["cost"].get("resources"), list)
            for payload in confirmation_payloads
        )
        successful_quote_result = any(
            (item.get("eventType") == "tool_result" or item.get("event_type") == "tool_result")
            and isinstance(item.get("data"), dict)
            and item["data"].get("toolName") == "ros_estimate_template_cost"
            and item["data"].get("isError") is not True
            for _, item in _walk(values)
            if isinstance(item, dict)
        )
        if successful_quote_result:
            runtime.checks["successful ROS quote projected into confirmation"] = any(
                isinstance(payload, dict)
                and isinstance(payload.get("cost"), dict)
                and payload["cost"].get("quote_status") == "succeeded"
                and str(payload["cost"].get("monthly_estimate") or "") not in {"", "询价不可用", "询价失败"}
                for payload in confirmation_payloads
            )


def _pending_kind(a2a: Any, path: Path) -> str:
    return str(a2a._latest_pending_kind(path) or "")


def _a2a_turn(
    runtime: ScenarioRuntime,
    harness: Any,
    *,
    prompt: str,
    name: str,
    image_key: str = "",
) -> Any:
    runtime.event("a2a-turn-started", name=name, image=bool(image_key))
    if image_key:
        summary = harness.stream_image_text(text=prompt, image_key=image_key, name=name)
    else:
        summary = harness.stream(prompt=prompt, name=name)
    runtime.event(
        "a2a-turn-finished",
        name=name,
        contextId=summary.context_id,
        taskId=summary.task_id,
        inputRequiredStep=summary.last_input_required_step_id,
    )
    return summary


@dataclass
class A2AConversationPlan:
    ask_answers: list[str] = field(default_factory=list)
    candidate_answers: list[str] = field(default_factory=list)
    confirmation_answers: list[str] = field(default_factory=list)
    default_confirmation: str = "cancel"
    image_kinds: set[str] = field(default_factory=set)
    image_counts: dict[str, int] = field(default_factory=dict)


def _a2a_plan(runtime: ScenarioRuntime) -> A2AConversationPlan:
    profile = runtime.spec.profile
    plan = A2AConversationPlan(
        ask_answers=[
            "部署在 cn-hangzhou，使用低成本按量资源；继续生成可选架构。",
            f"使用测试网段 {runtime.cidr}，其它参数按最小成本推荐。",
        ],
        candidate_answers=[_candidate_payload(0)],
        confirmation_answers=[_confirmation_payload("cancel")],
    )
    if runtime.spec.cloud_write:
        plan.confirmation_answers = [_confirmation_payload("confirm")]
        plan.default_confirmation = "confirm"
    if profile in {"backup_restore", "input_during_backup"}:
        plan.ask_answers = [
            "我要在阿里云杭州复用已有 VPC 创建一个 VSwitch；先规划方案，实现阶段再向我询问 VPC ID。",
            runtime.args.cleanup_vpc_id or "请用 aliyun_api 只读查询并让我确认一个已有 VPC",
            runtime.args.cleanup_zone_id or "请使用杭州可用区和低成本默认值",
        ]
    if profile == "step1_clarify":
        plan.ask_answers = [
            "我要上线一个面向小团队的 Node.js 电商后端 API，部署在 cn-hangzhou，使用低成本按量资源；"
            "请继续生成可选架构。"
        ]
    elif profile == "step1_replace":
        plan.candidate_answers = [
            "先把当前方案改成私网最小化架构并重新展示，不要实现模板。",
            "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch；请替换旧目标重新规划。",
            _candidate_payload(0),
        ]
    elif profile == "structured_override":
        plan.candidate_answers = [_candidate_payload(0, with_ignored_override=True)]
        plan.confirmation_answers = [
            _confirmation_payload("adjust", {"CidrBlock": runtime.cidr}),
            _confirmation_payload("cancel"),
        ]
    elif profile == "reselect_new_intent":
        plan.candidate_answers = [_candidate_payload(0), _candidate_payload(1), _candidate_payload(0)]
        plan.confirmation_answers = [
            _confirmation_payload("reselect"),
            "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch，请重新规划。",
            _confirmation_payload("cancel"),
        ]
    elif profile.startswith("rollback"):
        plan.candidate_answers = [_candidate_payload(0), _candidate_payload(0)]
        plan.confirmation_answers = [
            "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch；替换原部署目标。",
            _confirmation_payload("confirm"),
        ]
    elif profile == "safe_cancel":
        # A02 is declared cloud_write=False, so it must never answer "confirm": the confirmation
        # gate is the only thing standing between Step 2 and a real cloud write, and safe mode does
        # not restrict step tools. The natural-language cancel is the case under test; the
        # structured cancel is only a deterministic fallback if the model re-asks.
        plan.confirmation_answers = [
            "取消本次部署，不创建任何资源。",
            _confirmation_payload("cancel"),
        ]
    elif profile == "early_exit":
        plan.ask_answers = ["我确认仍然只使用 AWS，不使用阿里云，也不生成或部署 ROS 模板。"]
    elif profile == "step2_parameter":
        vpc = runtime.args.cleanup_vpc_id or "请用 aliyun_api 从本账号已有 VPC 中选择一个"
        zone = runtime.args.cleanup_zone_id or "请用 aliyun_api 选择杭州可用区"
        plan.ask_answers = [vpc, zone, runtime.cidr]
    elif profile == "image_asks":
        plan.image_kinds = {"ask_user_question", "deployment_confirmation"}
        plan.confirmation_answers = [
            f"调整参数：将网段改为 {runtime.cidr}，重新 Preview 和询价。",
            _confirmation_payload("cancel"),
        ]
    elif profile == "image_interrupt":
        plan.image_kinds = {"deployment_confirmation"}
        plan.confirmation_answers = [
            "我改需求了：只创建安全组，请回到方案规划重新选择。",
            _confirmation_payload("confirm"),
        ]
        plan.candidate_answers = [_candidate_payload(0), _candidate_payload(0)]
    elif profile == "image_initial":
        plan.image_kinds = {"candidate_selection"}
        plan.candidate_answers = ["你随便选一个方案。"]
    elif profile == "legacy_smoke":
        plan.candidate_answers = ["取消本次流程，不部署任何资源。"]
    return plan


def _drive_a2a_waiting(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    *,
    before_response: Callable[[str, str, Any], None] | None = None,
) -> Any:
    initial_image = runtime.spec.profile == "image_initial"
    summary = _a2a_turn(
        runtime,
        harness,
        prompt=_initial_prompt(runtime),
        name="turn-00-initial",
        image_key="initial" if initial_image else "",
    )
    return _continue_a2a_from_summary(
        runtime,
        harness,
        a2a,
        plan,
        summary,
        before_response=before_response,
    )


def _run_a2a_legacy_smoke(runtime: ScenarioRuntime, harness: Any, a2a: Any) -> None:
    """Exercise legacy planning through its candidate boundary, then cancel the task safely."""
    plan = _a2a_plan(runtime)
    summary = _a2a_turn(
        runtime,
        harness,
        prompt=_initial_prompt(runtime),
        name="legacy-smoke-to-candidate-selection",
    )
    waiting_sequence: list[str] = []
    for turn_index in range(1, 5):
        kind = _pending_kind(a2a, runtime.paths.run_dir / f"{summary.name}.events.jsonl")
        step_id = str(getattr(summary, "last_input_required_step_id", "") or "")
        waiting_sequence.append(f"{step_id}:{kind}" if step_id else kind)
        if kind in {"candidate_selection", "candidate_select"}:
            break
        if kind != "ask_user_question":
            raise RuntimeError(f"legacy smoke expected clarification or candidate selection, got {kind!r}")
        answer = plan.ask_answers.pop(0) if plan.ask_answers else "按杭州地域低成本默认参数继续规划。"
        summary = _a2a_turn(
            runtime,
            harness,
            prompt=answer,
            name=f"legacy-smoke-clarification-{turn_index}",
        )
    else:  # pragma: no cover - the loop always exits via a terminal kind or raises
        kind = ""
    if kind not in {"candidate_selection", "candidate_select"}:
        raise RuntimeError(f"legacy smoke expected candidate selection, got {kind!r}")
    cancel_result = harness.cancel_pipeline_task("legacy-smoke-cancel-at-candidate-selection")
    if isinstance(cancel_result, dict) and cancel_result.get("error"):
        raise RuntimeError("legacy smoke task cancellation failed")
    runtime.checks["A2A task identity persisted"] = bool(harness.context_id and harness.pipeline_task_id)
    runtime.checks["A2A waiting input was exercised"] = True
    runtime.checks["legacy canceled at candidate selection"] = True
    write_json(runtime.paths.artifacts_dir / "waiting-sequence.json", waiting_sequence)


def _continue_a2a_from_summary(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    summary: Any,
    *,
    before_response: Callable[[str, str, Any], None] | None = None,
) -> Any:
    seen_waiting: list[str] = []
    for turn_index in range(1, 18):
        if runtime.cancel_event.is_set():
            raise InterruptedError("suite cancellation requested")
        _raise_for_unexpected_a2a_terminal(summary)
        if bool(getattr(summary, "normal_handoff_ready", False)) or a2a._pipeline_completed(summary):
            break
        path = runtime.paths.run_dir / f"{summary.name}.events.jsonl"
        kind = _pending_kind(a2a, path)
        step_id = str(getattr(summary, "last_input_required_step_id", "") or "")
        if not kind:
            # A rejected/early-exit pipeline may already have handed off without a pending input.
            if runtime.spec.profile == "early_exit":
                break
            response = "继续"
        else:
            response, image_key = _a2a_response_for_pending(runtime, kind, plan)
        if kind:
            seen_waiting.append(f"{step_id}:{kind}")
        if before_response is not None and kind:
            before_response(step_id, kind, summary)
        if not kind:
            image_key = ""
        summary = _a2a_turn(
            runtime,
            harness,
            prompt=response,
            name=f"turn-{turn_index:02d}-{kind}",
            image_key=image_key,
        )
    else:
        raise RuntimeError("A2A conversation exceeded the bounded 18-turn state machine")
    runtime.checks["A2A task identity persisted"] = bool(harness.context_id and harness.pipeline_task_id)
    runtime.checks["A2A waiting input was exercised"] = (
        bool(runtime.checks.get("A2A waiting input was exercised"))
        or bool(seen_waiting)
        or runtime.spec.profile == "early_exit"
    )
    write_json(runtime.paths.artifacts_dir / "waiting-sequence.json", seen_waiting)
    return summary


def _raise_for_unexpected_a2a_terminal(summary: Any) -> None:
    state = str(getattr(summary, "last_status_state", "") or "")
    if state not in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
        return
    detail = str(getattr(summary, "text", "") or "").strip()
    if len(detail) > 500:
        detail = detail[-500:]
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"A2A task entered unexpected terminal state {state}{suffix}")


def _a2a_response_for_pending(
    runtime: ScenarioRuntime,
    kind: str,
    plan: A2AConversationPlan,
) -> tuple[str, str]:
    if kind == "ask_user_question":
        response = plan.ask_answers.pop(0) if plan.ask_answers else "使用低成本默认值继续。"
    elif kind in {"candidate_select", "candidate_selection"}:
        response = plan.candidate_answers.pop(0) if plan.candidate_answers else _candidate_payload(0)
    elif kind == "deployment_confirmation":
        response = (
            plan.confirmation_answers.pop(0)
            if plan.confirmation_answers
            else _confirmation_payload(plan.default_confirmation)
        )
    else:
        raise RuntimeError(f"unsupported pending input kind {kind!r} in {runtime.spec.name}")
    normalized_kind = "candidate_selection" if kind == "candidate_select" else kind
    image_key = ""
    if normalized_kind in plan.image_kinds:
        image_index = plan.image_counts.get(normalized_kind, 0)
        image_limit = 2 if runtime.spec.profile == "image_asks" and normalized_kind == "ask_user_question" else 1
        if image_index < image_limit:
            image_key = {
                "ask_user_question": "ask-first-answer" if image_index == 0 else "ask-second-answer",
                "candidate_selection": "selection",
                "deployment_confirmation": (
                    "rollback-interrupt" if runtime.spec.profile == "image_interrupt" else "confirmation-adjust"
                ),
            }.get(normalized_kind, "")
            plan.image_counts[normalized_kind] = image_index + 1
    return response, image_key


def _advance_a2a_to_pending(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    target_kind: str,
    *,
    name_prefix: str,
    seen_waiting: list[str] | None = None,
) -> Any:
    summary = _a2a_turn(runtime, harness, prompt=_initial_prompt(runtime), name=f"{name_prefix}-initial")
    for index in range(12):
        kind = _pending_kind(a2a, runtime.paths.run_dir / f"{summary.name}.events.jsonl")
        step_id = str(getattr(summary, "last_input_required_step_id", "") or "")
        if kind and seen_waiting is not None:
            seen_waiting.append(f"{step_id}:{kind}")
        normalized_kind = "candidate_selection" if kind == "candidate_select" else kind
        normalized_target = "candidate_selection" if target_kind == "candidate_select" else target_kind
        if normalized_kind == normalized_target:
            return summary
        if not kind:
            raise RuntimeError(f"pipeline completed before pending input {target_kind}")
        response, image_key = _a2a_response_for_pending(runtime, kind, plan)
        summary = _a2a_turn(
            runtime,
            harness,
            prompt=response,
            name=f"{name_prefix}-advance-{index:02d}-{kind}",
            image_key=image_key,
        )
    raise RuntimeError(f"pipeline did not reach pending input {target_kind}")


def _continue_a2a_to_pending(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    summary: Any,
    target_kind: str,
    *,
    name_prefix: str,
) -> Any:
    for index in range(12):
        kind = _pending_kind(a2a, runtime.paths.run_dir / f"{summary.name}.events.jsonl")
        normalized_kind = "candidate_selection" if kind == "candidate_select" else kind
        normalized_target = "candidate_selection" if target_kind == "candidate_select" else target_kind
        if normalized_kind == normalized_target:
            return summary
        if not kind:
            summary = _a2a_turn(
                runtime,
                harness,
                prompt="继续恢复未完成步骤。",
                name=f"{name_prefix}-resume-{index:02d}",
            )
            continue
        response, image_key = _a2a_response_for_pending(runtime, kind, plan)
        summary = _a2a_turn(
            runtime,
            harness,
            prompt=response,
            name=f"{name_prefix}-{index:02d}-{kind}",
            image_key=image_key,
        )
    raise RuntimeError(f"recovered task did not reach pending input {target_kind}")


def _start_a2a_step(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    target_step: str,
    *,
    name_prefix: str,
) -> Any:
    if target_step == NEW_STEPS[0]:
        background = harness.start_stream(prompt=_initial_prompt(runtime), name=f"{name_prefix}-step1")
    elif target_step == NEW_STEPS[1]:
        _advance_a2a_to_pending(
            runtime,
            harness,
            a2a,
            plan,
            "candidate_selection",
            name_prefix=f"{name_prefix}-to-selection",
        )
        response, image_key = _a2a_response_for_pending(runtime, "candidate_selection", plan)
        background = harness.start_stream(
            prompt=response,
            name=f"{name_prefix}-step2",
            images=[harness.image_fixtures.part(image_key, response)] if image_key else None,
        )
    elif target_step == NEW_STEPS[2]:
        _advance_a2a_to_pending(
            runtime,
            harness,
            a2a,
            plan,
            "deployment_confirmation",
            name_prefix=f"{name_prefix}-to-confirmation",
        )
        response = _confirmation_payload("confirm")
        background = harness.start_stream(prompt=response, name=f"{name_prefix}-step3")
    else:  # pragma: no cover - internal caller contract
        raise ValueError(target_step)
    background.wait_for(
        a2a._step_started(target_step),
        description=f"{target_step} started",
        timeout=runtime.args.timeout,
    )
    return background


def _rollback_new_intent(runtime: ScenarioRuntime) -> str:
    return (
        "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch；请替换旧目标重新规划。"
        f"最终 ROS StackName 仍必须使用 {runtime.stack_name}。"
    )


def _run_a2a_rollback_recovery(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    target_step: str,
) -> None:
    confirmation = _advance_a2a_to_pending(
        runtime,
        harness,
        a2a,
        plan,
        "deployment_confirmation",
        name_prefix="rollback-before",
    )
    del confirmation
    new_intent = _rollback_new_intent(runtime)
    if plan.confirmation_answers:
        plan.confirmation_answers.pop(0)
    step1_stream = harness.start_stream(prompt=new_intent, name="rollback-new-intent-step1")
    step1_stream.wait_for(
        a2a._step_started(NEW_STEPS[0]),
        description="rollback Step 1 started",
        timeout=runtime.args.timeout,
    )
    active_stream = step1_stream
    if target_step == NEW_STEPS[0]:
        harness.kill9_and_restart()
    else:
        active_stream.join(timeout=runtime.args.stream_timeout)
        step1_summary = active_stream.summary
        selection = _continue_a2a_to_pending(
            runtime,
            harness,
            a2a,
            plan,
            step1_summary,
            "candidate_selection",
            name_prefix="rollback-to-selection",
        )
        del selection
        selected_response, _ = _a2a_response_for_pending(runtime, "candidate_selection", plan)
        step2_stream = harness.start_stream(prompt=selected_response, name="rollback-selected-step2")
        step2_stream.wait_for(
            a2a._step_started(NEW_STEPS[1]),
            description="rollback Step 2 started",
            timeout=runtime.args.timeout,
        )
        active_stream = step2_stream
        if target_step == NEW_STEPS[1]:
            harness.kill9_and_restart()
        else:
            active_stream.join(timeout=runtime.args.stream_timeout)
            step2_summary = active_stream.summary
            _continue_a2a_to_pending(
                runtime,
                harness,
                a2a,
                plan,
                step2_summary,
                "deployment_confirmation",
                name_prefix="rollback-to-confirmation",
            )
            step3_stream = harness.start_stream(
                prompt=_confirmation_payload("confirm"),
                name="rollback-confirmed-step3",
            )
            step3_stream.wait_for(
                a2a._step_started(NEW_STEPS[2]),
                description="rollback Step 3 started",
                timeout=runtime.args.timeout,
            )
            active_stream = step3_stream
            harness.kill9_and_restart()
    runtime.event("server-restarted", checkpoint=f"rollback-{target_step}")
    with contextlib.suppress(Exception):
        active_stream.join(timeout=5)
    recovered = harness.stream(prompt="继续恢复回滚后的当前步骤。", name=f"rollback-recover-{target_step}")
    runtime.checks[f"rollback {target_step} restored same task"] = recovered.task_id == harness.pipeline_task_id
    _continue_a2a_from_summary(runtime, harness, a2a, plan, recovered)


def _backup_restore_hook(
    runtime: ScenarioRuntime, harness: Any, a2a: Any
) -> tuple[Callable[[str, str, Any], None], set[str]]:
    restored: set[str] = set()

    def restore(step_id: str, kind: str, _summary: Any) -> None:
        normalized = "candidate_selection" if kind == "candidate_select" else kind
        key = f"{step_id}:{normalized}"
        expected = {
            f"{NEW_STEPS[0]}:ask_user_question",
            f"{NEW_STEPS[0]}:candidate_selection",
            f"{NEW_STEPS[1]}:ask_user_question",
            f"{NEW_STEPS[1]}:deployment_confirmation",
        }
        if key not in expected or key in restored:
            return
        snapshot = harness.fetch_state(f"backup-before-{len(restored) + 1}")
        write_json(runtime.paths.snapshots_dir / f"backup-before-{len(restored) + 1}.json", snapshot)
        cwd, session_id = a2a._pipeline_session_identity(harness)
        primary_storage = a2a.SessionStorage(projects_dir=runtime.paths.config_dir / "projects")
        backup_storage = a2a.SessionStorage(projects_dir=runtime.paths.backup_dir / "projects")
        deadline = time.monotonic() + runtime.args.timeout
        backup_session = None
        while time.monotonic() < deadline:
            backup_session = backup_storage.v2_session_dir(cwd, session_id)
            if backup_session is not None and backup_session.is_dir():
                break
            time.sleep(0.25)
        if backup_session is None or not backup_session.is_dir():
            raise RuntimeError(f"backup session was not written for {key}")
        primary_session = primary_storage.v2_session_dir(cwd, session_id)
        if primary_session is None or not primary_session.is_dir():
            raise RuntimeError(f"primary session is unavailable for {key}")
        primary_resolved = primary_session.resolve()
        config_projects = (runtime.paths.config_dir / "projects").resolve()
        if config_projects not in primary_resolved.parents or primary_resolved.name != session_id:
            raise RuntimeError("refusing to remove a primary session outside the isolated case config")
        harness.kill9()
        shutil.rmtree(primary_resolved)
        if primary_resolved.exists() or not backup_session.is_dir():
            raise RuntimeError("failed to establish backup-only recovery state")
        harness.start_server()
        restored.add(key)
        runtime.event("backup-restored", pending=key, sessionId=session_id)

    return restore, restored


def _run_a2a_backup_restore(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
) -> None:
    hook, restored = _backup_restore_hook(runtime, harness, a2a)
    _drive_a2a_waiting(runtime, harness, a2a, plan, before_response=hook)
    expected = {
        f"{NEW_STEPS[0]}:ask_user_question",
        f"{NEW_STEPS[0]}:candidate_selection",
        f"{NEW_STEPS[1]}:ask_user_question",
        f"{NEW_STEPS[1]}:deployment_confirmation",
    }
    runtime.checks["all four waiting states restored from backup"] = restored == expected
    write_json(runtime.paths.artifacts_dir / "backup-restore-checkpoints.json", {"restored": sorted(restored)})


def _backup_delay_marker(control: Path, marker: str) -> Path:
    return control.with_name(f"{control.name}.{marker}.json")


def _arm_a2a_backup_delay(runtime: ScenarioRuntime, harness: Any, a2a: Any, checkpoint: int) -> Path:
    control = runtime.paths.artifacts_dir / f"backup-delay-{checkpoint:02d}"
    fixture_root = Path(a2a.BACKUP_DELAY_FIXTURE_ROOT).resolve()
    existing_pythonpath = harness.server_env.get("PYTHONPATH", "")
    pythonpath_parts = [str(fixture_root)]
    pythonpath_parts.extend(
        part for part in existing_pythonpath.split(os.pathsep) if part and part != str(fixture_root)
    )
    harness.server_env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_SECONDS"] = str(a2a.BACKUP_DELAY_SECONDS)
    # Directory mode lets one long-lived server claim each numbered arm file
    # exactly once. This avoids inserting a synthetic recovery message between
    # the four pending-input boundaries covered by A10.
    harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] = str(runtime.paths.artifacts_dir)
    write_json(
        _backup_delay_marker(control, "arm"),
        {"checkpoint": checkpoint, "armedAt": time.time(), "delaySeconds": a2a.BACKUP_DELAY_SECONDS},
    )
    return control


def _input_required_kind_and_step(a2a: Any, expected_step: str, expected_kind: str) -> Callable[[Any, Any], bool]:
    normalized_expected = "candidate_selection" if expected_kind == "candidate_select" else expected_kind

    def predicate(event: Any, _summary: Any) -> bool:
        for envelope in a2a._extract_pipeline_envelopes(event):
            if envelope.get("eventType") != "input_required":
                continue
            step = envelope.get("step")
            data = envelope.get("data")
            step_id = str(step.get("id") or "") if isinstance(step, dict) else ""
            if isinstance(data, dict):
                step_id = str(data.get("stepId") or step_id)
                kind = str(data.get("kind") or "")
            else:
                kind = ""
            normalized_kind = "candidate_selection" if kind == "candidate_select" else kind
            if step_id == expected_step and normalized_kind == normalized_expected:
                return True
        return False

    return predicate


def _input_received_kind_and_step(a2a: Any, expected_step: str, expected_kind: str) -> Callable[[Any, Any], bool]:
    normalized_expected = "candidate_selection" if expected_kind == "candidate_select" else expected_kind

    def predicate(event: Any, _summary: Any) -> bool:
        for envelope in a2a._extract_pipeline_envelopes(event):
            if envelope.get("eventType") != "input_received":
                continue
            step = envelope.get("step")
            data = envelope.get("data")
            step_id = str(step.get("id") or "") if isinstance(step, dict) else ""
            if not isinstance(data, dict):
                continue
            step_id = str(data.get("stepId") or step_id)
            kind = str(data.get("kind") or "")
            normalized_kind = "candidate_selection" if kind == "candidate_select" else kind
            if step_id == expected_step and normalized_kind == normalized_expected:
                return True
        return False

    return predicate


def _input_received_after_sequence_kind_and_step(
    a2a: Any,
    minimum_sequence: int,
    expected_step: str,
    expected_kind: str,
) -> Callable[[Any, Any], bool]:
    matches_identity = _input_received_kind_and_step(a2a, expected_step, expected_kind)

    def predicate(event: Any, summary: Any) -> bool:
        if not matches_identity(event, summary):
            return False
        return any(
            envelope.get("eventType") == "input_received"
            and int(float(envelope.get("sequence") or 0)) > minimum_sequence
            for envelope in a2a._extract_pipeline_envelopes(event)
        )

    return predicate


def _input_required_after_sequence(a2a: Any, minimum_sequence: int) -> Callable[[Any, Any], bool]:
    def predicate(event: Any, _summary: Any) -> bool:
        return any(
            envelope.get("eventType") == "input_required"
            and int(float(envelope.get("sequence") or 0)) > minimum_sequence
            for envelope in a2a._extract_pipeline_envelopes(event)
        )

    return predicate


def _input_required_after_sequence_kind_and_step(
    a2a: Any,
    minimum_sequence: int,
    expected_step: str,
    expected_kind: str,
) -> Callable[[Any, Any], bool]:
    matches_identity = _input_required_kind_and_step(a2a, expected_step, expected_kind)

    def predicate(event: Any, summary: Any) -> bool:
        if not matches_identity(event, summary):
            return False
        return any(
            envelope.get("eventType") == "input_required"
            and int(float(envelope.get("sequence") or 0)) > minimum_sequence
            for envelope in a2a._extract_pipeline_envelopes(event)
        )

    return predicate


def _event_type_max_sequence(a2a: Any, event: Any, event_type: str) -> int:
    return max(
        (
            int(float(envelope.get("sequence") or 0))
            for envelope in a2a._extract_pipeline_envelopes(event)
            if envelope.get("eventType") == event_type
        ),
        default=0,
    )


def _pending_step_and_kind(a2a: Any, event: Any) -> tuple[str, str]:
    for envelope in a2a._extract_pipeline_envelopes(event):
        if envelope.get("eventType") != "input_required":
            continue
        step = envelope.get("step")
        data = envelope.get("data")
        step_id = str(step.get("id") or "") if isinstance(step, dict) else ""
        if not isinstance(data, dict):
            continue
        step_id = str(data.get("stepId") or step_id)
        kind = str(data.get("kind") or "")
        return step_id, "candidate_selection" if kind == "candidate_select" else kind
    return "", ""


def _first_pending_resource_option_id(a2a: Any, event: Any) -> str:
    for envelope in a2a._extract_pipeline_envelopes(event):
        if envelope.get("eventType") != "input_required":
            continue
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        options = data.get("options")
        if not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("id"), str):
                continue
            option_id = option["id"].strip()
            if re.match(r"^(?:vpc|vsw|sg|i|eip|lb)-[A-Za-z0-9]+$", option_id) or re.match(
                r"^cn-[a-z0-9-]+$", option_id
            ):
                return option_id
    return ""


def _pending_from_pipeline_state(value: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(value, dict):
        return "", "", {}
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, dict):
        return "", "", {}
    pending = snapshot.get("pendingInput")
    if not isinstance(pending, dict):
        return "", "", {}
    step = pending.get("step")
    step_id = str(step.get("id") or "") if isinstance(step, dict) else ""
    kind = str(pending.get("kind") or "")
    return step_id, "candidate_selection" if kind == "candidate_select" else kind, pending


def _first_pending_resource_option_id_from_data(pending: dict[str, Any]) -> str:
    options = pending.get("options")
    if not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("id"), str):
            continue
        option_id = option["id"].strip()
        if re.match(r"^(?:vpc|vsw|sg|i|eip|lb)-[A-Za-z0-9]+$", option_id) or re.match(
            r"^cn-[a-z0-9-]+$", option_id
        ):
            return option_id
    return ""


def _run_a2a_input_during_backup(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    first_control: Path,
) -> None:
    expected = (
        (NEW_STEPS[0], "ask_user_question"),
        (NEW_STEPS[0], "candidate_selection"),
        (NEW_STEPS[1], "ask_user_question"),
        (NEW_STEPS[1], "deployment_confirmation"),
    )
    current = harness.start_stream(
        prompt=_initial_prompt(runtime),
        name="backup-window-01-initial",
        context_id="",
        task_id="",
    )
    control = first_control
    control_index = 1
    last_pending_sequence = 0
    evidence: list[dict[str, Any]] = []
    supplemental_evidence: list[dict[str, Any]] = []
    for index, (expected_step, expected_kind) in enumerate(expected, start=1):
        while True:
            # The input_required envelope is deliberately publication-gated by the
            # critical backup. Waiting for that envelope before dispatching the
            # response would necessarily miss the backup window. The mirrored
            # pipeline snapshot is already authoritative at this point, so read it
            # after the delay marker and use its pendingInput to prepare the request.
            started = a2a._wait_for_backup_delay_marker(
                control,
                "started",
                # Reaching the next waiting boundary can include a real LLM turn.
                # The short delay-sized timeout only applies after the marker exists.
                timeout=runtime.args.timeout,
            )
            pending_state = harness.fetch_state(f"backup-window-{control_index:02d}-pending")
            observed_step, observed_kind, pending_data = _pending_from_pipeline_state(pending_state)
            if not observed_step or not observed_kind:
                raise RuntimeError("backup-window snapshot did not expose the pending input")
            matched_target = observed_step == expected_step and observed_kind == expected_kind
            supplemental_ask = (
                expected_step == NEW_STEPS[1]
                and expected_kind == "deployment_confirmation"
                and observed_step == NEW_STEPS[1]
                and observed_kind == "ask_user_question"
            )
            if not matched_target and not supplemental_ask:
                raise RuntimeError(f"expected {expected_step}:{expected_kind}, got {observed_step}:{observed_kind}")
            unfinished_at_dispatch = not _backup_delay_marker(control, "finished").exists()
            response, image_key = _a2a_response_for_pending(runtime, observed_kind, plan)
            if observed_step == NEW_STEPS[1] and observed_kind == "ask_user_question":
                response = _first_pending_resource_option_id_from_data(pending_data) or response
            response_stream = harness.start_stream(
                prompt=response,
                name=f"backup-window-{control_index:02d}-response-{observed_kind}",
                images=[harness.image_fixtures.part(image_key, response)] if image_key else None,
                wait_for_identity=False,
            )
            pending = current.wait_for(
                _input_required_after_sequence_kind_and_step(
                    a2a,
                    last_pending_sequence,
                    observed_step,
                    observed_kind,
                ),
                description=f"input_required while awaiting {expected_step}:{expected_kind}",
                timeout=runtime.args.stream_timeout,
            )
            event_step, event_kind = _pending_step_and_kind(a2a, pending.event)
            if (event_step, event_kind) != (observed_step, observed_kind):
                raise RuntimeError(
                    "backup-window snapshot/event pending input mismatch: "
                    f"{observed_step}:{observed_kind} != {event_step}:{event_kind}"
                )
            pending_sequence = _event_type_max_sequence(a2a, pending.event, "input_required")
            last_pending_sequence = pending_sequence
            finished = a2a._wait_for_backup_delay_marker(
                control,
                "finished",
                timeout=min(runtime.args.timeout, a2a.BACKUP_DELAY_SECONDS + 5),
            )
            started_monotonic = float(started.get("startedMonotonic") or 0.0)
            finished_monotonic = float(finished.get("finishedMonotonic") or 0.0)
            dispatched_monotonic = float(response_stream.request_started_monotonic or 0.0)
            dispatched_during_backup = (
                unfinished_at_dispatch
                and started_monotonic > 0
                and started_monotonic <= dispatched_monotonic < finished_monotonic
            )
            a2a._wait_any(
                [current, response_stream],
                _input_received_after_sequence_kind_and_step(
                    a2a,
                    pending_sequence,
                    observed_step,
                    observed_kind,
                ),
                description=f"{observed_kind} input consumed",
                timeout=runtime.args.stream_timeout,
            )
            observed_types = set(current.summary.pipeline_event_types) | set(
                response_stream.summary.pipeline_event_types
            )
            not_interrupt = not {"interrupt_received", "interrupt_classified"}.intersection(observed_types)
            item = {
                "stepId": observed_step,
                "kind": observed_kind,
                "delaySeconds": finished.get("elapsedSeconds"),
                "requestDispatchedDuringBackup": dispatched_during_backup,
                "consumedAsPendingInput": "input_received" in observed_types,
                "classifiedAsInterrupt": not not_interrupt,
            }
            if matched_target:
                evidence.append(item)
                runtime.checks[f"backup window {index} request dispatched during delay"] = dispatched_during_backup
                runtime.checks[f"backup window {index} consumed pending input"] = "input_received" in observed_types
                runtime.checks[f"backup window {index} avoided interrupt routing"] = not_interrupt
            else:
                supplemental_evidence.append(item)
            if matched_target and index == len(expected):
                summary = current.join(timeout=runtime.args.stream_timeout)
                _continue_a2a_from_summary(runtime, harness, a2a, plan, summary)
                break

            control_index += 1
            if control_index > 12:
                raise RuntimeError("too many supplemental pending inputs during backup-window coverage")
            control = _arm_a2a_backup_delay(runtime, harness, a2a, control_index)
            with contextlib.suppress(Exception):
                response_stream.join(timeout=5)
            if matched_target:
                break
        if matched_target and index == len(expected):
            break
    write_json(
        runtime.paths.artifacts_dir / "backup-input-checkpoints.json",
        {"checkpoints": evidence, "supplementalCheckpoints": supplemental_evidence},
    )
    runtime.checks["all four backup-window inputs verified"] = len(evidence) == 4 and all(
        item["requestDispatchedDuringBackup"] and item["consumedAsPendingInput"] and not item["classifiedAsInterrupt"]
        for item in evidence
    )
    runtime.checks["A2A waiting input was exercised"] = bool(evidence)


def _event_contains(*markers: str) -> Callable[[Any, Any], bool]:
    lowered_markers = tuple(marker.lower() for marker in markers)

    def predicate(event: Any, _summary: Any) -> bool:
        text = _json_text(event).lower()
        return all(marker in text for marker in lowered_markers)

    return predicate


def _successful_tool_result(a2a: Any, expected_tool_name: str) -> Callable[[Any, Any], bool]:
    def predicate(event: Any, _summary: Any) -> bool:
        for envelope in a2a._extract_pipeline_envelopes(event):
            if envelope.get("eventType") != "tool_result":
                continue
            data = envelope.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("toolName") == expected_tool_name and data.get("isError") is not True:
                return True
        return False

    return predicate


def _kill_restart_at(
    runtime: ScenarioRuntime,
    harness: Any,
    stream: Any,
    predicate: Callable[[Any, Any], bool],
    checkpoint: str,
) -> None:
    stream.wait_for(predicate, description=checkpoint, timeout=runtime.args.stream_timeout)
    harness.kill9()
    with contextlib.suppress(Exception):
        stream.join(timeout=5)
    harness.start_server()
    runtime.event("server-restarted", checkpoint=checkpoint)


def _run_a2a_fault_checkpoints(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
) -> None:
    checkpoints: list[str] = []
    stream = harness.start_stream(prompt=_initial_prompt(runtime), name="fault-snapshot")
    _kill_restart_at(
        runtime,
        harness,
        stream,
        a2a._step_started(NEW_STEPS[0]),
        "snapshot",
    )
    checkpoints.append("snapshot")

    recovered = harness.stream(prompt="继续恢复方案规划。", name="fault-after-snapshot")
    _continue_a2a_to_pending(
        runtime,
        harness,
        a2a,
        plan,
        recovered,
        "candidate_selection",
        name_prefix="fault-to-selection",
    )
    candidate_response, _ = _a2a_response_for_pending(runtime, "candidate_selection", plan)
    stream = harness.start_stream(prompt=candidate_response, name="fault-candidate-selected")
    _kill_restart_at(
        runtime,
        harness,
        stream,
        _input_received_kind_and_step(a2a, NEW_STEPS[0], "candidate_selection"),
        "candidate-selected",
    )
    checkpoints.append("candidate-selected")

    stream = harness.start_stream(prompt="继续恢复选中方案的实现。", name="fault-template")
    _kill_restart_at(runtime, harness, stream, _event_contains("validate", "template"), "template-written-validated")
    checkpoints.append("template-written-validated")

    stream = harness.start_stream(prompt="继续恢复并完成询价。", name="fault-quote")
    _kill_restart_at(
        runtime,
        harness,
        stream,
        _successful_tool_result(a2a, "ros_estimate_template_cost"),
        "quote-saved",
    )
    checkpoints.append("quote-saved")

    recovered = harness.stream(prompt="继续恢复到部署确认。", name="fault-after-quote")
    _continue_a2a_to_pending(
        runtime,
        harness,
        a2a,
        plan,
        recovered,
        "deployment_confirmation",
        name_prefix="fault-to-confirmation",
    )
    if plan.confirmation_answers:
        plan.confirmation_answers.pop(0)
    stream = harness.start_stream(prompt=_confirmation_payload("confirm"), name="fault-confirmation-saved")
    _kill_restart_at(runtime, harness, stream, _event_contains("input_received"), "confirmation-saved")
    checkpoints.append("confirmation-saved")

    stream = harness.start_stream(prompt="继续执行已确认部署。", name="fault-create-stack")
    _kill_restart_at(runtime, harness, stream, _event_contains("CreateStack", "StackId"), "create-stack-returned")
    checkpoints.append("create-stack-returned")

    final = harness.stream(prompt="继续等待原 Stack 完成，禁止创建第二个 Stack。", name="fault-final-recovery")
    _continue_a2a_from_summary(runtime, harness, a2a, plan, final)
    write_json(runtime.paths.artifacts_dir / "fault-checkpoints.json", {"checkpoints": checkpoints})
    runtime.checks["all six fault checkpoints exercised"] = len(checkpoints) == 6


def _run_a2a_rollback_cleanup(
    runtime: ScenarioRuntime,
    harness: Any,
    a2a: Any,
    plan: A2AConversationPlan,
    *,
    recover_cleanup: bool,
) -> None:
    base = runtime.stack_name[:61]
    first_name = f"{base}-a"[:64]
    second_name = f"{base}-b"[:64]
    runtime.owned_stack_names.update({first_name, second_name})
    runtime.stack_name = first_name
    _advance_a2a_to_pending(
        runtime,
        harness,
        a2a,
        plan,
        "deployment_confirmation",
        name_prefix="cleanup-first-to-confirmation",
    )
    if plan.confirmation_answers:
        plan.confirmation_answers.pop(0)
    first_deploy = harness.start_stream(
        prompt=_confirmation_payload("confirm"),
        name="cleanup-first-stack",
    )
    first_deploy.wait_for(
        _event_contains("CreateStack", "StackId"),
        description="first Stack observed",
        timeout=runtime.args.stream_timeout,
    )
    runtime.stack_name = second_name
    new_intent = (
        "我改需求了：停止旧目标，改为只创建一个安全组，不创建 VPC 或 VSwitch。"
        f"新 ROS StackName 必须是 {second_name}；请回滚并清理旧 Stack 后重新规划。"
    )
    rollback_stream = harness.start_stream(prompt=new_intent, name="cleanup-rollback-new-intent")
    rollback_stream.wait_for(
        _event_contains("rollback_completed"),
        description="post-stack rollback completed",
        timeout=runtime.args.stream_timeout,
    )
    if recover_cleanup:
        first_deploy.wait_for(
            _event_contains("cleanup_started"),
            description="rollback cleanup started",
            timeout=runtime.args.stream_timeout,
        )
        harness.kill9_and_restart()
        with contextlib.suppress(Exception):
            first_deploy.join(timeout=5)
        runtime.event("server-restarted", checkpoint="rollback-cleanup-started")
        recovered = harness.stream(prompt="继续恢复旧 Stack 清理和新目标规划。", name="cleanup-after-restart")
        current = recovered
    else:
        first_deploy.wait_for(
            a2a._step_started(NEW_STEPS[0]),
            description="post-stack rollback Step 1",
            timeout=runtime.args.stream_timeout,
        )
        first_deploy.join(timeout=runtime.args.stream_timeout)
        current = first_deploy.summary
    selection = _continue_a2a_to_pending(
        runtime,
        harness,
        a2a,
        plan,
        current,
        "candidate_selection",
        name_prefix="cleanup-second-to-selection",
    )
    del selection
    candidate_response, _ = _a2a_response_for_pending(runtime, "candidate_selection", plan)
    materialized = _a2a_turn(
        runtime,
        harness,
        prompt=candidate_response,
        name="cleanup-second-materialize",
    )
    confirmation = _continue_a2a_to_pending(
        runtime,
        harness,
        a2a,
        plan,
        materialized,
        "deployment_confirmation",
        name_prefix="cleanup-second-to-confirmation",
    )
    del confirmation
    final = _a2a_turn(
        runtime,
        harness,
        prompt=_confirmation_payload("confirm"),
        name="cleanup-second-deploy",
    )
    _continue_a2a_from_summary(runtime, harness, a2a, plan, final)
    runtime.checks["rollback cleanup used distinct StackNames"] = first_name != second_name


def _run_a2a(runtime: ScenarioRuntime) -> None:
    a2a = _legacy_a2a_module()
    harness = a2a.ScenarioHarness(_python_namespace(runtime), scenario=runtime.spec.name)
    _track_a2a_server_processes(runtime, harness)
    harness.server_env = runtime.env.copy()
    harness.cwd = str(runtime.paths.workspace_dir)
    harness.workspace_dir = runtime.paths.workspace_dir
    harness.port = runtime.port
    harness.server_url = f"http://127.0.0.1:{runtime.port}"
    plan = _a2a_plan(runtime)
    first_backup_control = (
        _arm_a2a_backup_delay(runtime, harness, a2a, 1) if runtime.spec.profile == "input_during_backup" else None
    )
    runtime.event("surface-started", surface="a2a", port=runtime.port)
    try:
        harness.start_server()
        profile = runtime.spec.profile
        if profile == "step1_clarify":
            seen_waiting: list[str] = []
            _advance_a2a_to_pending(
                runtime,
                harness,
                a2a,
                plan,
                "candidate_selection",
                name_prefix="clarify-to-selection",
                seen_waiting=seen_waiting,
            )
            runtime.checks["A2A task identity persisted"] = bool(harness.context_id and harness.pipeline_task_id)
            runtime.checks["A2A waiting input was exercised"] = bool(seen_waiting)
            write_json(runtime.paths.artifacts_dir / "waiting-sequence.json", seen_waiting)
        elif profile == "backup_restore":
            _run_a2a_backup_restore(runtime, harness, a2a, plan)
        elif profile == "input_during_backup":
            if first_backup_control is None:  # pragma: no cover - guarded by the profile branch above
                raise RuntimeError("backup delay fixture was not armed")
            _run_a2a_input_during_backup(runtime, harness, a2a, plan, first_backup_control)
        elif profile == "fault_checkpoints":
            _run_a2a_fault_checkpoints(runtime, harness, a2a, plan)
        elif profile in {"rollback_cleanup", "rollback_cleanup_recovery"}:
            _run_a2a_rollback_cleanup(
                runtime,
                harness,
                a2a,
                plan,
                recover_cleanup=profile == "rollback_cleanup_recovery",
            )
        elif profile in {"running_step1", "running_step2", "running_step3"}:
            target = {
                "running_step1": NEW_STEPS[0],
                "running_step2": NEW_STEPS[1],
                "running_step3": NEW_STEPS[2],
            }[profile]
            background = _start_a2a_step(
                runtime,
                harness,
                a2a,
                plan,
                target,
                name_prefix="running-recovery",
            )
            harness.kill9_and_restart()
            runtime.event("server-restarted", checkpoint=target)
            with contextlib.suppress(Exception):
                background.join(timeout=5)
            recovered = harness.stream(prompt="继续恢复当前步骤。", name=f"recover-{target}")
            runtime.checks[f"{target} restored same task"] = recovered.task_id == harness.pipeline_task_id
            _continue_a2a_from_summary(runtime, harness, a2a, plan, recovered)
            runtime.checks[f"{target} running recovery"] = True
        elif profile in {"cancel_step1", "cancel_step2", "cancel_step3"}:
            target = {"cancel_step1": NEW_STEPS[0], "cancel_step2": NEW_STEPS[1], "cancel_step3": NEW_STEPS[2]}[profile]
            background = _start_a2a_step(
                runtime,
                harness,
                a2a,
                plan,
                target,
                name_prefix="cancel-running",
            )
            harness.cancel_pipeline_task(f"cancel-{target}")
            with contextlib.suppress(Exception):
                background.join(timeout=5)
            runtime.checks[f"{target} cancel accepted"] = True
            cancel_text = _json_text(_all_event_values(runtime.paths.run_dir))
            runtime.checks[f"{target} reached canceled state"] = (
                "TASK_STATE_CANCELED" in cancel_text or "canceled" in cancel_text
            )
        elif profile in {"rollback_step1", "rollback_step2", "rollback_step3"}:
            target = {
                "rollback_step1": NEW_STEPS[0],
                "rollback_step2": NEW_STEPS[1],
                "rollback_step3": NEW_STEPS[2],
            }[profile]
            _run_a2a_rollback_recovery(runtime, harness, a2a, plan, target)
            runtime.checks[f"rollback recovery reached {target}"] = True
        elif profile == "normal_running":
            completed = _drive_a2a_waiting(runtime, harness, a2a, plan)
            runtime.checks["pipeline reached normal handoff"] = bool(completed.normal_handoff_ready)
            normal = harness.start_stream(
                prompt="请流式详细说明刚才的部署结果、架构与费用。",
                name="normal-running-before-restart",
                task_id="",
            )
            harness.kill9_and_restart()
            with contextlib.suppress(Exception):
                normal.join(timeout=5)
            followup = harness.stream(
                prompt="恢复后只回复 normal chat 历史仍然可用。",
                name="normal-running-after-restart",
                task_id="",
            )
            runtime.checks["normal running recovery kept context"] = followup.context_id == harness.context_id
            runtime.checks["normal running recovery used normal task"] = followup.task_id != harness.pipeline_task_id
        elif profile == "legacy_smoke":
            _run_a2a_legacy_smoke(runtime, harness, a2a)
        else:
            completed = _drive_a2a_waiting(runtime, harness, a2a, plan)
            if profile == "image_interrupt":
                normal = harness.stream_image_text(
                    text="你刚才创建了什么？请说明新方案、费用和 Stack 结果。",
                    image_key="normal-followup",
                    name="image-normal-followup",
                    task_id="",
                )
                runtime.checks["image handoff stayed in same context"] = normal.context_id == completed.context_id
                runtime.checks["image handoff used normal task"] = bool(normal.task_id) and (
                    normal.task_id != harness.pipeline_task_id
                )
                runtime.checks["image handoff produced text"] = bool(normal.text.strip())
        if harness.context_id and harness.pipeline_task_id:
            with contextlib.suppress(Exception):
                snapshot = harness.fetch_state("final-pipeline-state")
                write_json(runtime.paths.snapshots_dir / "final.json", snapshot)
            with contextlib.suppress(Exception):
                harness.capture_task_snapshots("final-task")
    finally:
        harness.terminate()
    values = _all_event_values(runtime.paths.run_dir)
    _common_pipeline_checks(runtime, values)
    runtime.checks["A2A public events captured"] = bool(values)
    runtime.checks["A2A requests captured"] = (runtime.paths.run_dir / "requests.jsonl").is_file()


REPL_SELECTION_PATTERNS = (
    r"请选择要实现并部署的方案",
    r"请输入您的选择",
    r"方案规划与选择.*\(1/3\)",
)
REPL_CONFIRMATION_PATTERNS = (
    r"请选择下一步操作",
    r"确认部署",
    r"询价概览",
)
REPL_CONFIRMATION_INPUT_READY_PATTERNS = (
    r"使用上下方向键选择；聚焦最后一行后可直接输入；按 Enter 确认。",
    r"Use Up/Down to select\. Type directly on the last row, then press Enter\.",
)
REPL_COMPLETED_PATTERNS = (
    r"Pipeline completed",
    r"Normal chat is now active",
    r"CREATE_COMPLETE",
    r"部署成功",
    r"已进入普通对话",
)
REPL_ASK_INPUT_READY_PATTERNS = (r"[ \t]+>[ \t]*(?:\x1b|$)",)
REPL_STACK_CREATED_PATTERNS = (r"CREATE_COMPLETE", r"Stack ID", r"StackId", r"stack_id")
REPL_CLEANUP_PATTERNS = (r"cleanup", r"回滚清理", r"DeleteStack", r"开始清理")


def _repl_select_current(pty: Any, *, next_candidate: bool = False) -> None:
    if next_candidate:
        pty.send("\x1b[C", label="candidate-right")
    pty.send("\r", label="candidate-enter")


def _repl_focus_confirmation_input(runtime: ScenarioRuntime, pty: Any) -> None:
    count = runtime.repl_confirmation_action_count
    if count <= 0:
        raise RuntimeError("deployment confirmation action count was not observed")
    for index in range(count):
        pty.send("\x1b[B", label=f"confirmation-input-down-{index + 1}")


def _repl_choose_direct_input(runtime: ScenarioRuntime, pty: Any, text: str) -> None:
    _repl_focus_confirmation_input(runtime, pty)
    pty.send(f"\x1b[200~{text}\x1b[201~", label="confirmation-direct-input-paste")
    time.sleep(0.1)
    pty.drain_output()
    pty.send("\r", label="confirmation-direct-input-enter")


def _repl_paste_generated_image(runtime: ScenarioRuntime, pty: Any, key: str, text: str) -> None:
    store = _legacy_a2a_module().TextImageFixtureStore(runtime.paths.run_dir / "image-fixtures")
    store.part(key, text)
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    image_path = str(manifest[key]["path"])
    pty.send(f"\x1b[200~{image_path}\x1b[201~", label=f"paste-image-{key}")
    pty.events.append({"type": "paste-image-fixture", "image_key": key, "path": image_path, "at": utc_now()})


def _repl_submit_image_fixture(pty: Any, key: str, *, label: str) -> None:
    pty.paste_image_fixture(key)
    time.sleep(0.1)
    pty.drain_output()
    pty.send("\r", label=label)


def _repl_submit_generated_image(
    runtime: ScenarioRuntime,
    pty: Any,
    key: str,
    text: str,
    *,
    label: str,
) -> None:
    _repl_paste_generated_image(runtime, pty, key, text)
    time.sleep(0.1)
    pty.drain_output()
    pty.send("\r", label=label)


def _repl_choose_direct_image(runtime: ScenarioRuntime, pty: Any, key: str, text: str) -> None:
    _repl_focus_confirmation_input(runtime, pty)
    _repl_paste_generated_image(runtime, pty, key, text)
    time.sleep(0.1)
    pty.drain_output()
    pty.send("\r", label="confirmation-direct-image-enter")


def _read_repl_display_events(runtime: ScenarioRuntime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((runtime.paths.config_dir / "projects").glob("*/*/pipeline/display.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    return events


def _write_repl_artifacts(runtime: ScenarioRuntime, pty: Any, repl: Any) -> None:
    raw = repl._redact_sensitive_text(pty.transcript, runtime.env)
    normalized = repl._normalize_transcript(raw)
    (runtime.paths.run_dir / "transcript.raw.log").write_text(raw, encoding="utf-8")
    (runtime.paths.run_dir / "transcript.normalized.log").write_text(normalized, encoding="utf-8")
    with (runtime.paths.run_dir / "repl-events.jsonl").open("w", encoding="utf-8") as handle:
        for event in pty.events:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    # Display events are ordered pipeline facts. Put them before the PTY-only
    # interaction records so Step 1/2 boundaries cannot be inferred from a
    # monolithic transcript that also contains later Preview/quote output.
    _common_pipeline_checks(runtime, _read_repl_display_events(runtime) + pty.events + [{"transcript": normalized}])
    runtime.checks["REPL transcript captured"] = bool(normalized.strip())
    unexpected_exit = any(
        event.get("type") == "terminate"
        and event.get("force") is not True
        and event.get("aliveBeforeTerminate") is False
        for event in pty.events
        if isinstance(event, dict)
    )
    runtime.checks["REPL stayed alive until teardown"] = not unexpected_exit
    runtime.checks["REPL has no terminal exception"] = not unexpected_exit and not any(
        _has_unhandled_terminal_error(event) for event in pty.events
    )


def _repl_wait_selection(pty: Any, runtime: ScenarioRuntime) -> None:
    runtime.repl_candidate_wait_count += 1
    event, path = _wait_repl_display_event(
        runtime,
        event_type="candidate_selection_ready",
        occurrence=runtime.repl_candidate_wait_count,
        timeout=runtime.args.stream_timeout,
        drain_output=getattr(pty, "drain_output", None),
    )
    pty.events.append(
        {
            "type": "display-event",
            "description": "selling_solution_first candidate selection",
            "event_type": event.get("type"),
            "occurrence": runtime.repl_candidate_wait_count,
            "path": str(path),
            "at": utc_now(),
        }
    )


def _repl_wait_step_started(
    pty: Any,
    runtime: ScenarioRuntime,
    *,
    step_id: str,
    occurrence: int,
    description: str,
) -> None:
    event, path = _wait_repl_display_event(
        runtime,
        event_type="step_started",
        occurrence=occurrence,
        timeout=runtime.args.stream_timeout,
        drain_output=getattr(pty, "drain_output", None),
        predicate=lambda item: item.get("step_id") == step_id,
    )
    pty.events.append(
        {
            "type": "display-event",
            "description": description,
            "event_type": event.get("type"),
            "step_id": step_id,
            "occurrence": occurrence,
            "path": str(path),
            "at": utc_now(),
        }
    )


def _repl_step_transcript_paths(runtime: ScenarioRuntime, step_id: str) -> list[Path]:
    """Return persisted parent-attempt transcripts belonging to ``step_id``."""

    paths: list[Path] = []
    for meta_path in sorted((runtime.paths.config_dir / "projects").glob("*/*/pipeline/meta.yaml")):
        try:
            metadata = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        attempts = metadata.get("attempts") if isinstance(metadata, dict) else None
        items = attempts.get("items") if isinstance(attempts, dict) else None
        if not isinstance(items, dict):
            continue
        for attempt in items.values():
            if not isinstance(attempt, dict) or attempt.get("step_id") != step_id:
                continue
            transcript_id = attempt.get("transcript_id")
            if not isinstance(transcript_id, str) or not transcript_id:
                continue
            path = meta_path.parent / "transcripts" / transcript_id / "session.jsonl"
            if path not in paths:
                paths.append(path)
    return paths


def _find_transcript_tool_use(path: Path, tool_names: set[str]) -> dict[str, Any] | None:
    for value in _read_json_lines(path):
        candidates = [value, *(item for _, item in _walk(value))]
        for item in candidates:
            if isinstance(item, dict) and item.get("type") == "tool_use" and item.get("name") in tool_names:
                return item
    return None


def _repl_reached_step2_confirmation(runtime: ScenarioRuntime) -> bool:
    return any(_is_repl_deployment_confirmation(event) for event in _read_repl_display_events(runtime))


def _repl_latest_terminal_display_event(runtime: ScenarioRuntime) -> dict[str, Any] | None:
    terminal_types = {"pipeline_user_aborted", "pipeline_failed", "backup_blocked", "pipeline_completed"}
    return next(
        (event for event in reversed(_read_repl_display_events(runtime)) if event.get("type") in terminal_types),
        None,
    )


def _wait_repl_transcript_tool_use(
    pty: Any,
    runtime: ScenarioRuntime,
    *,
    step_id: str,
    tool_names: set[str],
    description: str,
) -> None:
    """Wait for durable proof that a specific step is actively executing tools.

    Terminal rendering is intentionally not used here: Rich Live output may be
    drained before ``pexpect`` observes it. The parent-step transcript is the
    recovery source of truth and proves that the process was interrupted only
    after the target tool call had actually been persisted.
    """

    deadline = time.monotonic() + runtime.args.stream_timeout
    while time.monotonic() < deadline:
        pty.drain_output()
        terminal_event = _repl_latest_terminal_display_event(runtime)
        if terminal_event is not None:
            raise RuntimeError(
                f"REPL reached terminal display event {terminal_event.get('type')!r} before {description}"
            )
        # Do not mistake a tool call from a step that has already reached its
        # next waiting boundary for an in-flight checkpoint.
        if step_id == NEW_STEPS[1] and _repl_reached_step2_confirmation(runtime):
            raise RuntimeError(f"REPL reached deployment confirmation before {description}")
        for path in _repl_step_transcript_paths(runtime, step_id):
            tool_use = _find_transcript_tool_use(path, tool_names)
            if tool_use is None:
                continue
            pty.events.append(
                {
                    "type": "transcript-tool-use",
                    "description": description,
                    "step_id": step_id,
                    "tool_name": tool_use.get("name"),
                    "tool_use_id": tool_use.get("id"),
                    "path": str(path),
                    "at": utc_now(),
                }
            )
            return
        time.sleep(0.1)
    raise TimeoutError(
        f"timed out waiting for {description}; expected one of {sorted(tool_names)!r} "
        f"in persisted transcript for step {step_id!r}"
    )


def _repl_wait_ask(
    pty: Any,
    runtime: ScenarioRuntime,
    *,
    description: str,
    reject_confirmation: bool = False,
) -> None:
    # Question wording is model-generated and must not be constrained by a list
    # of Chinese keywords. The actual console-input prompt is the durable UI
    # boundary and also prevents an answer from racing the preceding key reader.
    patterns = REPL_ASK_INPUT_READY_PATTERNS + (REPL_CONFIRMATION_INPUT_READY_PATTERNS if reject_confirmation else ())
    matched = pty.expect_any(
        patterns,
        description=f"{description} input ready",
        timeout=runtime.args.stream_timeout,
    )
    if reject_confirmation and matched in REPL_CONFIRMATION_INPUT_READY_PATTERNS:
        raise RuntimeError(f"deployment confirmation appeared before {description}")
    # Cancelling the candidate key task cannot cancel a read_key() already
    # running in the executor. Simulate normal human reaction time so that
    # stale reader exits before the console-input answer is submitted.
    time.sleep(0.25)
    pty.drain_output()


def _repl_initial_input_recorded(runtime: ScenarioRuntime, text: str) -> bool:
    history_path = runtime.paths.config_dir / ".input_history"
    try:
        lines = history_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("text") == text:
            return True
    return False


def _repl_submit_initial_prompt(pty: Any, runtime: ScenarioRuntime) -> None:
    text = _initial_prompt(runtime)
    for attempt in range(1, 3):
        # Use bracketed paste so prompt-toolkit inserts the whole prompt with one
        # redraw. Character-by-character redraws can fill the PTY output buffer
        # before Enter is processed when the runner is not currently in expect().
        pty.send(f"\x1b[200~{text}\x1b[201~", label=f"initial-input-paste-{attempt}")
        pty.send("\r", label=f"initial-input-enter-{attempt}")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            pty.drain_output()
            if _repl_initial_input_recorded(runtime, text):
                pty.events.append({"type": "initial-input-accepted", "attempt": attempt, "at": utc_now()})
                return
            time.sleep(0.05)
    raise TimeoutError("REPL did not record the initial scenario input after two submissions")


def _repl_submit_line_input(pty: Any, text: str, *, label: str) -> None:
    """Submit restored prompt input without racing its line editor.

    ``pexpect.sendline`` can deliver Enter before prompt_toolkit consumes the
    final text bytes. Bracketed paste followed by a separately drained Enter
    uses the same reliable handoff as the initial prompt and candidate edit.
    """

    pty.send(f"\x1b[200~{text}\x1b[201~", label=f"{label}-paste")
    time.sleep(0.1)
    pty.drain_output()
    pty.send("\r", label=f"{label}-enter")


def _wait_repl_display_event(
    runtime: ScenarioRuntime,
    *,
    event_type: str,
    occurrence: int,
    timeout: float,
    drain_output: Callable[[], None] | None = None,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    check_before_drain: bool = False,
) -> tuple[dict[str, Any], Path]:
    deadline = time.monotonic() + timeout
    latest_count = 0
    terminal_types = {"pipeline_user_aborted", "pipeline_failed", "backup_blocked", "pipeline_completed"}
    while time.monotonic() < deadline:
        if drain_output is not None and not check_before_drain:
            drain_output()
        for path in sorted((runtime.paths.config_dir / "projects").glob("*/*/pipeline/display.jsonl")):
            matches: list[dict[str, Any]] = []
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            terminal_event: dict[str, Any] | None = None
            for line in lines:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("type") == event_type and (predicate is None or predicate(item)):
                    matches.append(item)
                if isinstance(item, dict) and item.get("type") in terminal_types:
                    terminal_event = item
            latest_count = max(latest_count, len(matches))
            if len(matches) >= occurrence:
                return matches[occurrence - 1], path
            if terminal_event is not None:
                raise RuntimeError(
                    f"REPL pipeline reached terminal display event {terminal_event.get('type')!r} "
                    f"before {event_type!r} occurrence {occurrence}"
                )
        if drain_output is not None and check_before_drain:
            drain_output()
        time.sleep(0.1)
    raise TimeoutError(
        f"timed out waiting for REPL display event {event_type!r} occurrence {occurrence}; observed {latest_count}"
    )


def _repl_submit_candidate_interrupt(pty: Any, runtime: ScenarioRuntime, text: str) -> None:
    repl = _legacy_repl_module()
    pty.send("\x1b", label="candidate-interrupt")
    repl._expect_interrupt_input_ready(
        pty,
        _python_namespace(runtime),
        visible_description="candidate selection interrupt visible",
        ready_description="candidate selection interrupt input ready",
    )
    # The canceled raw-key reader may still own stdin briefly after the prompt
    # readiness sequence. Match the ask path's human-sized handoff delay so the
    # architecture edit reaches the line editor rather than the stale reader.
    time.sleep(0.25)
    pty.drain_output()
    # A single pexpect.sendline(short_text) can deliver Enter before
    # prompt_toolkit has processed the final characters. Long text happened to
    # work because the shared helper chunked it first. Use bracketed paste and a
    # separate Enter for deterministic behavior regardless of text length.
    pty.send(f"\x1b[200~{text}\x1b[201~", label="candidate-interrupt-input")
    time.sleep(0.1)
    pty.drain_output()
    pty.send("\r", label="candidate-interrupt-enter")


def _repl_submit_pipeline_interrupt(pty: Any, runtime: ScenarioRuntime, text: str) -> None:
    """Interrupt active streaming and wait until its line editor owns stdin."""

    repl = _legacy_repl_module()
    pty.send("\x1b", label="pipeline-stream-interrupt")
    repl._expect_interrupt_input_ready(
        pty,
        _python_namespace(runtime),
        visible_description="pipeline stream interrupt visible",
        ready_description="pipeline stream interrupt input ready",
    )
    time.sleep(0.25)
    pty.drain_output()
    _repl_submit_line_input(pty, text, label="pipeline-stream-interrupt-input")


def _is_repl_deployment_confirmation(event: dict[str, Any]) -> bool:
    payload = event.get("payload")
    return (
        event.get("step_id") == NEW_STEPS[1]
        and isinstance(payload, dict)
        and payload.get("kind") == "deployment_confirmation"
    )


def _record_repl_confirmation_options(runtime: ScenarioRuntime, event: dict[str, Any]) -> None:
    payload = event.get("payload")
    options = payload.get("options") if isinstance(payload, dict) else None
    if not isinstance(options, list) or not options:
        raise RuntimeError("deployment confirmation display event has no action options")
    runtime.repl_confirmation_action_count = len(options)


def _prepare_restored_repl_confirmation(pty: Any, runtime: ScenarioRuntime) -> None:
    pty.expect_any(
        REPL_CONFIRMATION_INPUT_READY_PATTERNS,
        description="restored deployment confirmation selector ready",
        timeout=runtime.args.stream_timeout,
    )
    time.sleep(0.25)
    pty.drain_output()
    confirmation_events = [
        event for event in _read_repl_display_events(runtime) if _is_repl_deployment_confirmation(event)
    ]
    if not confirmation_events:
        raise RuntimeError("restored deployment confirmation display event was not observed")
    _record_repl_confirmation_options(runtime, confirmation_events[-1])


def _repl_wait_confirmation(pty: Any, runtime: ScenarioRuntime, *, require_input_ready: bool = True) -> None:
    runtime.repl_confirmation_wait_count += 1
    event, path = _wait_repl_display_event(
        runtime,
        event_type="user_input_required",
        occurrence=runtime.repl_confirmation_wait_count,
        timeout=runtime.args.stream_timeout,
        drain_output=getattr(pty, "drain_output", None),
        predicate=_is_repl_deployment_confirmation,
        check_before_drain=True,
    )
    # The display record is written before the REPL renders the confirmation.
    # Normal flows can wait for the selector's terminal frame. Recovery flows
    # may have already emitted and drained that transient frame while polling
    # the durable display journal, so they use a short human-sized handoff delay
    # instead of waiting forever for text that cannot be replayed.
    if require_input_ready:
        pty.expect_any(
            REPL_CONFIRMATION_INPUT_READY_PATTERNS,
            description=f"deployment confirmation selector ready #{runtime.repl_confirmation_wait_count}",
            timeout=runtime.args.stream_timeout,
        )
        time.sleep(0.25)
    else:
        time.sleep(0.5)
    pty.drain_output()
    _record_repl_confirmation_options(runtime, event)
    pty.events.append(
        {
            "type": "display-event",
            "description": "selling_solution_first deployment confirmation",
            "event_type": event.get("type"),
            "occurrence": runtime.repl_confirmation_wait_count,
            "path": str(path),
            "at": utc_now(),
        }
    )


def _repl_wait_confirmation_after_optional_parameter_asks(pty: Any, runtime: ScenarioRuntime) -> None:
    """Answer legitimate Step 2 parameter asks before the confirmation boundary."""

    for ask_index in range(1, 4):
        matched = pty.expect_any(
            REPL_ASK_INPUT_READY_PATTERNS + REPL_CONFIRMATION_INPUT_READY_PATTERNS,
            description=f"post-rollback Step 2 ask or confirmation #{ask_index}",
            timeout=runtime.args.stream_timeout,
        )
        if matched in REPL_CONFIRMATION_INPUT_READY_PATTERNS:
            # The readiness line was consumed above; use the durable display
            # record without trying to match the same transient hint twice.
            _repl_wait_confirmation(pty, runtime, require_input_ready=False)
            return
        time.sleep(0.25)
        pty.drain_output()
        answer = runtime.args.cleanup_vpc_id or "请使用上面列出的第一个可用杭州 VPC"
        _repl_submit_line_input(pty, answer, label=f"post-rollback-parameter-answer-{ask_index}")
    raise RuntimeError("post-rollback Step 2 did not reach deployment confirmation after three parameter asks")


def _repl_wait_pipeline_completed(pty: Any, runtime: ScenarioRuntime) -> None:
    event, path = _wait_repl_display_event(
        runtime,
        event_type="pipeline_completed",
        occurrence=1,
        timeout=runtime.args.stream_timeout,
        drain_output=getattr(pty, "drain_output", None),
    )
    pty.events.append(
        {
            "type": "display-event",
            "description": "selling_solution_first pipeline completed",
            "event_type": event.get("type"),
            "path": str(path),
            "at": utc_now(),
        }
    )


def _repl_step1_replan_prompt(runtime: ScenarioRuntime) -> str:
    return (
        f"请把方案改为只创建一个空 VPC，网段使用 {runtime.cidr}；"
        "不创建 VSwitch、安全组、ECS 或公网资源。更新架构图和详情后重新让我选择。"
    )


def _repl_step1_clarification_answer(runtime: ScenarioRuntime) -> str:
    return (
        "请先为一个最小测试 Web 应用规划网络和计算：在杭州新建 VPC、VSwitch、安全组和 1 台无公网 ECS，"
        f"使用低成本配置，预留网段为 {runtime.cidr}；可用区、实例规格和公共镜像可自动选择。"
    )


def _repl_basic_flow(runtime: ScenarioRuntime, pty: Any) -> None:
    profile = runtime.spec.profile
    _repl_submit_initial_prompt(pty, runtime)
    if profile == "step1_clarify":
        _repl_wait_ask(pty, runtime, description="pipeline question")
        pty.sendline(_repl_step1_clarification_answer(runtime))
    _repl_wait_selection(pty, runtime)
    if profile == "step1_clarify":
        _repl_submit_candidate_interrupt(
            pty,
            runtime,
            _repl_step1_replan_prompt(runtime),
        )
        _repl_wait_selection(pty, runtime)
    if profile == "replace_invalid":
        # Invalid numeric shortcuts are rejected locally and keep the current
        # candidate selector open; they do not produce a second display event.
        pty.send("9", label="candidate-invalid")
        time.sleep(0.25)
        pty.drain_output()
        _repl_submit_candidate_interrupt(
            pty,
            runtime,
            "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch。",
        )
        _repl_wait_selection(pty, runtime)
    _repl_select_current(pty, next_candidate=profile in {"natural_adjust", "reselect_progress"})
    if profile == "step2_parameter":
        _repl_wait_ask(
            pty,
            runtime,
            description="Step 2 VPC parameter question",
            reject_confirmation=True,
        )
        pty.sendline(runtime.args.cleanup_vpc_id or "请从账号内已有 VPC 中自动选择测试可用项")
        _repl_wait_ask(
            pty,
            runtime,
            description="Step 2 zone parameter question",
            reject_confirmation=True,
        )
        pty.sendline(runtime.args.cleanup_zone_id or "cn-hangzhou-h")
    _repl_wait_confirmation(pty, runtime)
    if profile == "natural_adjust":
        _repl_choose_direct_input(runtime, pty, f"把 VSwitch 网段调整为 {runtime.cidr}，重新 Preview 和询价。")
        _repl_wait_confirmation(pty, runtime)
        _repl_choose_direct_input(runtime, pty, "确认部署，参数覆盖保持刚才的值。")
    elif profile == "reselect_progress":
        _repl_choose_direct_input(runtime, pty, "重新选择方案")
        _repl_wait_selection(pty, runtime)
        _repl_select_current(pty, next_candidate=True)
        _repl_wait_confirmation(pty, runtime)
        _repl_choose_direct_input(runtime, pty, "取消")
    elif runtime.spec.cloud_write:
        # Confirm is the first option, so Enter avoids relying on natural-language parsing.
        pty.send("\r", label="confirmation-confirm")
    else:
        _repl_choose_direct_input(runtime, pty, "取消本次部署，不创建任何云资源。")


def _restart_repl_at_waiting(pty: Any, patterns: tuple[str, ...], runtime: ScenarioRuntime, label: str) -> None:
    if patterns == REPL_SELECTION_PATTERNS:
        _repl_wait_selection(pty, runtime)
        pty.terminate(force=True)
        pty.spawn(extra_args=["--continue"])
        _repl_wait_selection(pty, runtime)
        # The durable display event proves the restored renderer consumed the
        # selection boundary. Its Live hint is transient and may already have
        # been drained by the journal poll, so allow the cbreak reader to settle
        # instead of matching already-consumed terminal text.
        time.sleep(0.5)
        pty.drain_output()
        return
    pty.expect_any(patterns, description=f"{label} before restart", timeout=runtime.args.stream_timeout)
    pty.terminate(force=True)
    pty.spawn(extra_args=["--continue"])
    if patterns == REPL_CONFIRMATION_PATTERNS:
        # The selector-ready hint is printed before the first "confirm" option.
        # Matching a broad confirmation pattern first consumes that hint, so
        # wait for the exact readiness marker directly after restart.
        _prepare_restored_repl_confirmation(pty, runtime)
        return
    pty.expect_any(patterns, description=f"{label} restored", timeout=runtime.args.stream_timeout)
    if patterns == REPL_ASK_INPUT_READY_PATTERNS:
        time.sleep(0.25)
        pty.drain_output()


def _run_repl_waiting_resume_all(runtime: ScenarioRuntime, pty: Any) -> None:
    _repl_submit_initial_prompt(pty, runtime)
    _restart_repl_at_waiting(pty, REPL_ASK_INPUT_READY_PATTERNS, runtime, "Step 1 ask")
    _repl_submit_line_input(
        pty,
        "在杭州复用已有 VPC 创建一个 VSwitch；实现阶段再询问 VPC ID。",
        label="restored-step1-ask-answer",
    )
    _restart_repl_at_waiting(pty, REPL_SELECTION_PATTERNS, runtime, "candidate selection")
    _repl_select_current(pty)
    _restart_repl_at_waiting(pty, REPL_ASK_INPUT_READY_PATTERNS, runtime, "Step 2 parameter ask")
    _repl_submit_line_input(
        pty,
        runtime.args.cleanup_vpc_id or "请只读查询账号已有 VPC 并使用测试可用项",
        label="restored-step2-ask-answer",
    )
    _restart_repl_at_waiting(pty, REPL_CONFIRMATION_PATTERNS, runtime, "deployment confirmation")
    _repl_choose_direct_input(runtime, pty, "取消，不创建任何云资源。")
    runtime.checks["all four REPL waiting states resumed"] = (
        sum(
            "--continue" in [str(item) for item in event.get("command", [])]
            for event in pty.events
            if event.get("type") == "spawn"
        )
        == 4
    )


def _run_repl_interrupt_rollback(runtime: ScenarioRuntime, pty: Any) -> None:
    _repl_submit_initial_prompt(pty, runtime)
    _repl_wait_selection(pty, runtime)
    _repl_select_current(pty)
    _repl_wait_confirmation(pty, runtime)
    _repl_choose_direct_input(runtime, pty, "我改需求了：只创建安全组，不创建 VPC 或 VSwitch；请重新规划。")
    _repl_wait_selection(pty, runtime)
    _repl_select_current(pty)
    _repl_wait_confirmation_after_optional_parameter_asks(pty, runtime)
    pty.send("\r", label="confirmation-confirm")
    _wait_repl_transcript_tool_use(
        pty,
        runtime,
        step_id=NEW_STEPS[2],
        tool_names={"ros_deploy"},
        description="interrupt rollback persisted deployment tool checkpoint",
    )
    _repl_submit_pipeline_interrupt(
        pty,
        runtime,
        "架构再次变化：改为只创建一个空 VPC，不创建安全组；请重新规划。",
    )
    _repl_wait_selection(pty, runtime)
    _repl_select_current(pty)
    _repl_wait_confirmation_after_optional_parameter_asks(pty, runtime)
    _repl_choose_direct_input(runtime, pty, "取消，不再部署。")
    runtime.checks["REPL Step 2 and Step 3 rollback inputs submitted"] = True


def _run_repl_cleanup_recovery(runtime: ScenarioRuntime, pty: Any) -> None:
    _repl_submit_initial_prompt(pty, runtime)
    _repl_wait_selection(pty, runtime)
    _repl_select_current(pty)
    _repl_wait_confirmation(pty, runtime)
    pty.send("\r", label="confirmation-confirm")
    pty.expect_any(REPL_STACK_CREATED_PATTERNS, description="first Stack observed", timeout=runtime.args.stream_timeout)
    pty.send("\x1b", label="post-stack-rollback")
    pty.sendline("我改需求了：只创建安全组，请重新规划并部署新目标。")
    pty.expect_any(REPL_CLEANUP_PATTERNS, description="rollback cleanup started", timeout=runtime.args.stream_timeout)
    pty.terminate(force=True)
    pty.spawn(extra_args=["--continue"])
    pty.expect_any(
        REPL_CLEANUP_PATTERNS + REPL_SELECTION_PATTERNS,
        description="cleanup recovery restored",
        timeout=runtime.args.stream_timeout,
    )
    runtime.checks["REPL cleanup resumed with --continue"] = True


def _run_repl_multimodal_lifecycle(runtime: ScenarioRuntime, pty: Any) -> None:
    _repl_submit_generated_image(
        runtime,
        pty,
        "initial",
        "选择一个已有 VPC 创建一个 VSwitch。架构规划阶段先直接给出方案；"
        "方案选定后、写模板前必须列出可用 VPC 并向我提问，由我选择，不能代选。"
        "可用区和网段可以推荐合法且低成本的默认值。",
        label="initial-image-enter",
    )
    _repl_wait_selection(pty, runtime)
    _repl_submit_image_fixture(pty, "selection", label="selection-image-enter")
    _repl_wait_multimodal_confirmation(
        runtime,
        pty,
        primary_image_key="ask-first-answer",
        primary_image_text=(
            "选择问题列表中的第一个已有 VPC，继续创建 VSwitch；"
            "可用区和网段使用低成本且合法的默认值，不要再次询问。"
        ),
        phase="initial",
    )

    _repl_choose_direct_image(
        runtime,
        pty,
        "confirmation-adjust",
        f"把 VSwitch 网段调整为 {runtime.cidr}，重新 Preview、询价并更新方案说明。",
    )
    _repl_wait_multimodal_confirmation(
        runtime,
        pty,
        primary_image_key="ask-second-answer",
        phase="adjustment",
    )
    _repl_choose_direct_image(
        runtime,
        pty,
        "rollback-interrupt",
        "我改需求了：使用已有 VPC 创建一个安全组，不创建 VSwitch。请重新规划。",
    )
    _repl_wait_multimodal_selection(runtime, pty, phase="rollback")
    _repl_submit_image_fixture(pty, "selection", label="rollback-selection-image-enter")
    _repl_wait_multimodal_confirmation(
        runtime,
        pty,
        primary_image_key="rollback-ask-answer",
        primary_image_text="选择问题列表中的第一个已有 VPC，继续创建安全组；不创建 VSwitch 或其他资源。",
        phase="rollback",
    )
    _repl_choose_direct_input(runtime, pty, "取消，不创建任何云资源。")
    pty.expect_any(
        REPL_COMPLETED_PATTERNS, description="multimodal pipeline handoff", timeout=runtime.args.stream_timeout
    )
    _legacy_repl_module()._expect_initial_prompt(pty, _python_namespace(runtime))
    _repl_submit_image_fixture(pty, "normal-followup", label="normal-followup-image-enter")
    pty.expect_any(
        (r"安全组", r"方案", r"取消", r"没有创建"),
        description="normal image follow-up response",
        timeout=runtime.args.stream_timeout,
    )
    observed_keys = {
        str(event.get("image_key"))
        for event in pty.events
        if isinstance(event, dict) and event.get("type") == "paste-image-fixture"
    }
    runtime.checks["REPL full image lifecycle exercised"] = {
        "initial",
        "ask-first-answer",
        "selection",
        "confirmation-adjust",
        "rollback-interrupt",
        "normal-followup",
    }.issubset(observed_keys)


def _repl_wait_multimodal_selection(runtime: ScenarioRuntime, pty: Any, *, phase: str) -> None:
    """Answer legitimate Step 1 asks with images until candidates are ready."""

    repl = _legacy_repl_module()
    target_occurrence = runtime.repl_candidate_wait_count + 1
    scan_offset = len(pty.transcript)
    ask_index = 0
    deadline = time.monotonic() + runtime.args.stream_timeout
    while time.monotonic() < deadline:
        pty.drain_output()
        candidate_count = sum(
            event.get("type") == "candidate_selection_ready" for event in _read_repl_display_events(runtime)
        )
        if candidate_count >= target_occurrence:
            _repl_wait_selection(pty, runtime)
            return

        transcript = pty.transcript
        suffix = transcript[scan_offset:]
        permission_pattern = next(
            (pattern for pattern in repl.PERMISSION_PROMPT_PATTERNS if re.search(pattern, suffix)),
            None,
        )
        if permission_pattern is not None:
            scan_offset = len(transcript)
            response_mode = getattr(getattr(pty, "args", None), "permission_prompt_response", "pageup-enter")
            pty.send(
                repl._permission_prompt_response_sequence(response_mode),
                label="permission-prompt-response",
            )
            deadline = time.monotonic() + runtime.args.stream_timeout
            continue

        if any(re.search(pattern, suffix) for pattern in REPL_ASK_INPUT_READY_PATTERNS):
            ask_index += 1
            if ask_index > 4:
                raise RuntimeError(f"{phase} multimodal Step 1 asked more than four questions")
            scan_offset = len(transcript)
            pty.events.append(
                {
                    "type": "runner-detected-ask",
                    "description": f"{phase} image Step 1 ask #{ask_index}",
                    "at": utc_now(),
                }
            )
            time.sleep(0.25)
            pty.drain_output()
            _repl_submit_generated_image(
                runtime,
                pty,
                f"{phase}-step1-answer-{ask_index}",
                "选择问题列表中的第一个已有 VPC，继续规划安全组；不创建 VSwitch 或其他资源。",
                label=f"{phase}-step1-image-ask-enter-{ask_index}",
            )
            scan_offset = len(pty.transcript)
            deadline = time.monotonic() + runtime.args.stream_timeout
            continue
        time.sleep(0.1)
    raise TimeoutError(f"{phase} multimodal Step 1 did not reach candidate selection before timeout")


def _repl_wait_multimodal_confirmation(
    runtime: ScenarioRuntime,
    pty: Any,
    *,
    primary_image_key: str,
    primary_image_text: str | None = None,
    phase: str,
) -> None:
    """Answer one or more legitimate Step 2 asks with image inputs."""

    for ask_index in range(1, 5):
        matched = pty.expect_any(
            REPL_ASK_INPUT_READY_PATTERNS + REPL_CONFIRMATION_INPUT_READY_PATTERNS,
            description=f"{phase} image ask or confirmation #{ask_index}",
            timeout=runtime.args.stream_timeout,
        )
        if matched in REPL_CONFIRMATION_INPUT_READY_PATTERNS:
            _repl_wait_confirmation(pty, runtime, require_input_ready=False)
            return
        time.sleep(0.25)
        pty.drain_output()
        if ask_index == 1:
            label = f"{phase}-image-ask-enter-{ask_index}"
            if primary_image_text:
                _repl_submit_generated_image(
                    runtime,
                    pty,
                    primary_image_key,
                    primary_image_text,
                    label=label,
                )
            else:
                _repl_submit_image_fixture(pty, primary_image_key, label=label)
        else:
            _repl_paste_generated_image(
                runtime,
                pty,
                f"{phase}-parameter-{ask_index}",
                "请直接选择问题选项中的第一个默认 VPC，并继续；"
                "后续可用区和网段使用低成本且合法的默认值，不要再次询问。",
            )
            pty.send("\r", label=f"{phase}-image-ask-enter-{ask_index}")
    raise RuntimeError(f"{phase} multimodal Step 2 did not reach confirmation after four parameter asks")


def _run_repl(runtime: ScenarioRuntime) -> None:
    repl = _legacy_repl_module()
    pty = repl.ReplPty(
        args=_python_namespace(runtime),
        run_dir=runtime.paths.run_dir,
        cwd=runtime.paths.workspace_dir,
        env=runtime.env,
    )
    runtime.event("surface-started", surface="repl")
    try:
        pty.spawn()
        # The prompt-toolkit REPL can discard bytes sent while the welcome screen is
        # still initializing. Reuse the legacy runner's readiness handshake before
        # submitting the first scenario prompt.
        repl._expect_initial_prompt(pty, _python_namespace(runtime))
        time.sleep(0.25)
        profile = runtime.spec.profile
        if profile == "waiting_resume":
            _run_repl_waiting_resume_all(runtime, pty)
        elif profile in {"running_step1", "running_step2", "running_step3"}:
            _repl_submit_initial_prompt(pty, runtime)
            target_step = {
                "running_step1": NEW_STEPS[0],
                "running_step2": NEW_STEPS[1],
                "running_step3": NEW_STEPS[2],
            }[profile]
            if profile in {"running_step2", "running_step3"}:
                _repl_wait_selection(pty, runtime)
                _repl_select_current(pty)
                if profile == "running_step3":
                    _repl_wait_confirmation(pty, runtime)
                    pty.send("\r", label="confirmation-confirm")
            _repl_wait_step_started(
                pty,
                runtime,
                step_id=target_step,
                occurrence=1,
                description=f"{profile} initial running checkpoint",
            )
            if profile == "running_step2":
                _wait_repl_transcript_tool_use(
                    pty,
                    runtime,
                    step_id=target_step,
                    tool_names={"write_file"},
                    description="running_step2 persisted template tool checkpoint",
                )
            elif profile == "running_step3":
                _wait_repl_transcript_tool_use(
                    pty,
                    runtime,
                    step_id=target_step,
                    tool_names={"ros_deploy"},
                    description="running_step3 persisted deployment tool checkpoint",
                )
            pty.terminate(force=True)
            pty.spawn(extra_args=["--continue"])
            runtime.checks["REPL used --continue"] = True
            if profile == "running_step1":
                _repl_wait_selection(pty, runtime)
                time.sleep(0.5)
                pty.drain_output()
                _repl_select_current(pty)
                _repl_wait_confirmation(pty, runtime)
                if runtime.spec.cloud_write:
                    pty.send("\r", label="confirmation-confirm")
                else:
                    _repl_choose_direct_input(runtime, pty, "取消，不创建任何云资源。")
            elif profile == "running_step2":
                _repl_wait_confirmation(pty, runtime)
                if runtime.spec.cloud_write:
                    pty.send("\r", label="confirmation-confirm")
                else:
                    _repl_choose_direct_input(runtime, pty, "取消，不创建任何云资源。")
            _repl_wait_pipeline_completed(pty, runtime)
            runtime.checks[f"{target_step} auto-continued after --continue"] = True
        elif profile == "normal_resume":
            _repl_basic_flow(runtime, pty)
            pty.expect_any(REPL_COMPLETED_PATTERNS, description="pipeline handoff", timeout=runtime.args.stream_timeout)
            pty.sendline("请详细解释刚才的部署结果。")
            pty.send("\x03", label="normal-chat-ctrl-c")
            pty.sendline("请只回复 normal chat 仍可用。")
            runtime.checks["normal chat remained usable after Ctrl+C"] = True
        elif profile == "interrupt_rollback":
            _run_repl_interrupt_rollback(runtime, pty)
        elif profile == "cleanup_recovery":
            _run_repl_cleanup_recovery(runtime, pty)
        elif profile == "multimodal":
            _run_repl_multimodal_lifecycle(runtime, pty)
        else:
            _repl_basic_flow(runtime, pty)
            _repl_wait_pipeline_completed(pty, runtime)
    finally:
        pty.terminate()
        _write_repl_artifacts(runtime, pty, repl)


def _contains_text(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_text(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_text(item, expected) for item in value)
    return isinstance(value, str) and expected in value


def _wait_web_state(
    web: Any, base_url: str, web_session_id: str, predicate: Callable[[Any], bool], timeout: float
) -> Any:
    deadline = time.monotonic() + timeout
    latest: Any = {}
    while time.monotonic() < deadline:
        # The session detail endpoint only exposes persisted Web-session metadata.
        # Pipeline recovery state (including pendingInput) is hydrated by /status.
        latest = web._json_request(base_url, "GET", web._session_path(web_session_id, "/status"))
        terminal_failure = _web_terminal_failure_status(latest)
        if terminal_failure:
            raise RuntimeError(f"Web pipeline reached terminal status {terminal_failure!r} while waiting for state")
        if predicate(latest):
            return latest
        time.sleep(0.25)
    raise TimeoutError(
        "timed out waiting for Web pipeline state "
        f"(status={latest.get('status')!r}, pending={_web_pending_kind(latest)!r})"
    )


def _web_terminal_failure_status(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    pipeline = value.get("pipeline")
    if not isinstance(pipeline, dict):
        return ""
    snapshot = pipeline.get("snapshot")
    candidates = [pipeline, snapshot] if isinstance(snapshot, dict) else [pipeline]
    failure_statuses = {"failed", "canceled", "cancelled", "aborted", "user_aborted", "blocked"}
    for candidate in candidates:
        status = str(candidate.get("status") or "").strip().lower()
        if status in failure_statuses:
            return status
        handoff = candidate.get("normalHandoff")
        if isinstance(handoff, dict):
            for key in ("status", "outcome"):
                handoff_status = str(handoff.get(key) or "").strip().lower()
                if handoff_status in failure_statuses:
                    return handoff_status
    return ""


def _wait_web_idle(web: Any, base_url: str, web_session_id: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    observed_running = False
    while time.monotonic() < deadline:
        latest = web._json_request(base_url, "GET", web._session_path(web_session_id, "/status"))
        if latest.get("status") == "running":
            observed_running = True
        elif observed_running or (
            time.monotonic() - started >= 1.0 and latest.get("status") not in {"queued", "starting"}
        ):
            return latest
        time.sleep(0.25)
    raise TimeoutError("timed out waiting for Web session to become idle")


def _web_pending_kind(value: Any) -> str:
    for key, item in _walk(value):
        if key in {"pendingInput", "pending_input"} and isinstance(item, dict):
            kind = item.get("kind")
            if isinstance(kind, str):
                return kind
    return ""


def _web_at_confirmation_boundary(value: Any) -> bool:
    return _web_pending_kind(value) in {"deployment_confirmation", "ask_user_question"}


def _web_at_materialize_boundary(value: Any) -> bool:
    """Stop polling on every Step 2 user boundary, including an unexpected rollback."""
    return _web_pending_kind(value) in {
        "deployment_confirmation",
        "ask_user_question",
        "candidate_selection",
        "candidate_select",
    }


def _web_w02_ask_answer(value: Any) -> str:
    """Answer W02 parameter questions without accidentally replacing its deployment goal."""
    pipeline = value.get("pipeline") if isinstance(value, dict) else None
    candidates: list[Any] = []
    if isinstance(pipeline, dict):
        candidates.append(pipeline.get("waitingInput"))
        snapshot = pipeline.get("snapshot")
        if isinstance(snapshot, dict):
            candidates.append(snapshot.get("pendingInput"))
    options: list[Any] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("kind") != "ask_user_question":
            continue
        data = candidate.get("data")
        source = data if isinstance(data, dict) else candidate
        raw_options = source.get("options")
        if isinstance(raw_options, list):
            options = raw_options
            break

    normalized = [item for item in options if isinstance(item, dict) and str(item.get("id") or "").strip()]
    # When the selected VPC already contains the proposed CIDR, explicitly keep
    # the original "create a VSwitch" goal. Choosing the "use existing" option
    # is an architecture change and legitimately triggers a rollback to Step 1.
    selected = next((item for item in normalized if item.get("id") == "create-new-vswitch"), None)
    if selected is None:
        selected = next(
            (
                item
                for item in normalized
                if re.match(r"^(?:vpc|vsw|sg|i|eip|lb)-[A-Za-z0-9]+$", str(item.get("id") or ""))
                or re.match(r"^cn-[a-z0-9-]+$", str(item.get("id") or ""))
            ),
            None,
        )
    if selected is None and normalized:
        selected = normalized[0]
    if selected is None:
        return "保持当前已选方案和部署目标不变，请采用本问题推荐的低成本默认值继续。"
    option_id = str(selected["id"]).strip()
    label = str(selected.get("label") or option_id).strip()
    return f"选择“{label}”（{option_id}），保持当前已选方案和部署目标不变。"


def _web_replacement_intent_prompt(*, multimodal: bool) -> str:
    if multimodal:
        return "请读取图片中的全新部署意图，并用它完整替换旧目标后重新规划。"
    return "我改需求了：不再创建当前方案，只创建安全组，请按这个全新目标重新规划。"


def _upload_web_fixture(web: Any, base_url: str, web_session_id: str, fixture_name: str) -> dict[str, Any]:
    fixture = REPO_ROOT / "scripts" / "a2a" / "e2e" / "fixtures" / "text-images" / f"{fixture_name}.png"
    if not fixture.is_file():
        raise FileNotFoundError(f"missing Web multimodal fixture: {fixture}")
    return web._json_request(
        base_url,
        "POST",
        web._session_path(web_session_id, "/images"),
        {
            "name": fixture.name,
            "mediaType": "image/png",
            "data": base64.b64encode(fixture.read_bytes()).decode("ascii"),
        },
    )


def _select_web_candidate(
    web: Any,
    base_url: str,
    model_session_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    # Candidate selection synchronously runs Step 2 until its next input boundary.
    # Unlike ordinary Web metadata requests, this can legitimately take minutes.
    return web._json_request(
        base_url,
        "POST",
        "/api/pipeline/candidates/select",
        {"sessionId": model_session_id, "candidateIndex": 0, "parameterOverrides": {}},
        timeout=timeout,
    )


def _web_session_create_payload(runtime: ScenarioRuntime) -> dict[str, Any]:
    return {
        "cwd": str(runtime.paths.workspace_dir),
        "mode": "pipeline",
        "pipelineName": PIPELINE_NAME,
        "provider": runtime.env.get("IAC_CODE_PROVIDER", ""),
        "model": runtime.env.get("IAC_CODE_MODEL", ""),
        # Web PermissionMode values are not CLI aliases. ``danger`` silently
        # normalizes to ``default`` and can leave an unattended E2E waiting on
        # a tool permission forever.
        "permissionMode": WEB_E2E_PERMISSION_MODE,
    }


def _run_web(runtime: ScenarioRuntime) -> None:
    web = _web_module()
    args = _python_namespace(runtime)
    base_url = f"http://127.0.0.1:{runtime.port}"
    runtime.event("surface-started", surface="web", port=runtime.port)
    process = web._start_web_server(args, runtime.paths.run_dir, runtime.port, runtime.env, epoch="web")
    runtime.register_process(process)
    payloads: list[Any] = []
    try:
        web._wait_for_health(base_url, process, timeout=runtime.args.timeout)
        created = web._json_request(
            base_url,
            "POST",
            "/api/sessions",
            _web_session_create_payload(runtime),
        )
        payloads.append(created)
        if created.get("permissionMode") != WEB_E2E_PERMISSION_MODE:
            raise RuntimeError(
                "Web session did not preserve unattended E2E permission mode: "
                f"expected {WEB_E2E_PERMISSION_MODE!r}, got {created.get('permissionMode')!r}"
            )
        web_session_id = str(created["webSessionId"])
        model_session_id = str(created["sessionId"])
        runtime.event("web-session-created", webSessionId=web_session_id, sessionId=model_session_id)
        initial_message: dict[str, Any] = {"text": _initial_prompt(runtime)}
        if runtime.spec.multimodal:
            upload = _upload_web_fixture(web, base_url, web_session_id, "initial")
            payloads.append(upload)
            initial_message = {
                "text": "请读取图片文字，并将图片内容作为初始部署需求执行。",
                "imageIds": [upload["imageId"]],
            }
        accepted = web._json_request(
            base_url,
            "POST",
            web._session_path(web_session_id, "/messages"),
            initial_message,
        )
        payloads.append(accepted)
        state = _wait_web_state(
            web,
            base_url,
            web_session_id,
            lambda value: _web_pending_kind(value) in {"candidate_selection", "candidate_select", "ask_user_question"},
            runtime.args.stream_timeout,
        )
        payloads.append(state)
        if _web_pending_kind(state) == "ask_user_question":
            ask_message: dict[str, Any] = {"text": f"使用杭州地域、低成本配置和网段 {runtime.cidr}。"}
            if runtime.spec.multimodal:
                upload = _upload_web_fixture(web, base_url, web_session_id, "ask-first-answer")
                payloads.append(upload)
                ask_message = {
                    "text": "请读取图片文字作为本轮问题的回答。",
                    "imageIds": [upload["imageId"]],
                }
            payloads.append(
                web._json_request(
                    base_url,
                    "POST",
                    web._session_path(web_session_id, "/messages"),
                    ask_message,
                )
            )
            _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
            state = _wait_web_state(
                web,
                base_url,
                web_session_id,
                lambda value: _web_pending_kind(value) in {"candidate_selection", "candidate_select"},
                runtime.args.stream_timeout,
            )
            payloads.append(state)
        # First reload contract: the persisted waiting state must survive a server epoch.
        web._stop_web_server(process, timeout=runtime.args.timeout)
        process = web._start_web_server(
            args, runtime.paths.run_dir, runtime.port, runtime.env, epoch="web-reload-selection"
        )
        runtime.register_process(process)
        web._wait_for_health(base_url, process, timeout=runtime.args.timeout)
        reloaded = web._json_request(base_url, "GET", web._session_path(web_session_id, "/status"))
        payloads.append(reloaded)
        runtime.checks["Web candidate waiting survived refresh"] = _web_pending_kind(reloaded) in {
            "candidate_selection",
            "candidate_select",
        }
        selection = _select_web_candidate(
            web,
            base_url,
            model_session_id,
            timeout=runtime.args.stream_timeout,
        )
        payloads.append(selection)
        _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
        confirmation = _wait_web_state(
            web,
            base_url,
            web_session_id,
            _web_at_materialize_boundary,
            runtime.args.stream_timeout,
        )
        payloads.append(confirmation)
        while _web_pending_kind(confirmation) == "ask_user_question":
            answer = (
                _web_w02_ask_answer(confirmation)
                if runtime.spec.case_id == "W02"
                else runtime.args.cleanup_vpc_id or f"使用已有资源和网段 {runtime.cidr}。"
            )
            payloads.append(
                web._json_request(
                    base_url,
                    "POST",
                    web._session_path(web_session_id, "/messages"),
                    {"text": answer},
                )
            )
            _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
            confirmation = _wait_web_state(
                web,
                base_url,
                web_session_id,
                _web_at_materialize_boundary,
                runtime.args.stream_timeout,
            )
            payloads.append(confirmation)
        if _web_pending_kind(confirmation) in {"candidate_selection", "candidate_select"}:
            raise RuntimeError("Step 2 unexpectedly rolled back before deployment confirmation")
        web._stop_web_server(process, timeout=runtime.args.timeout)
        process = web._start_web_server(
            args, runtime.paths.run_dir, runtime.port, runtime.env, epoch="web-reload-confirm"
        )
        runtime.register_process(process)
        web._wait_for_health(base_url, process, timeout=runtime.args.timeout)
        # A restored pending-input snapshot can become visible slightly before
        # startup recovery releases the Web turn reservation.  Wait for that
        # reservation to settle before posting the replacement intent, or the
        # otherwise valid request can race with recovery and receive HTTP 409.
        reloaded_confirmation = _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
        payloads.append(reloaded_confirmation)
        runtime.checks["Web confirmation survived refresh"] = (
            _web_pending_kind(reloaded_confirmation) == "deployment_confirmation"
        )
        if runtime.spec.profile == "full_flow":
            responses = [
                f"调整参数：使用预留网段 {runtime.cidr}，重新 Preview、询价并更新方案说明。",
                "重新选择方案",
            ]
            for response in responses:
                payloads.append(
                    web._json_request(
                        base_url,
                        "POST",
                        web._session_path(web_session_id, "/messages"),
                        {"text": response},
                    )
                )
                _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
                state = _wait_web_state(
                    web,
                    base_url,
                    web_session_id,
                    lambda value: (
                        _web_pending_kind(value)
                        in {
                            "candidate_selection",
                            "candidate_select",
                            "deployment_confirmation",
                        }
                    ),
                    runtime.args.stream_timeout,
                )
                payloads.append(state)
            if _web_pending_kind(state) in {"candidate_selection", "candidate_select"}:
                payloads.append(
                    _select_web_candidate(
                        web,
                        base_url,
                        model_session_id,
                        timeout=runtime.args.stream_timeout,
                    )
                )
                _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
                _wait_web_state(
                    web,
                    base_url,
                    web_session_id,
                    lambda value: _web_pending_kind(value) == "deployment_confirmation",
                    runtime.args.stream_timeout,
                )
            payloads.append(
                web._json_request(
                    base_url,
                    "POST",
                    web._session_path(web_session_id, "/messages"),
                    {"text": _confirmation_payload("confirm")},
                )
            )
            _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
            completed_state = _wait_web_state(
                web,
                base_url,
                web_session_id,
                lambda value: (
                    _contains_text(value, "pipeline_handoff_ready")
                    or _contains_text(value, "pipeline_completed")
                    or _contains_text(value, "CREATE_COMPLETE")
                ),
                runtime.args.stream_timeout,
            )
            payloads.append(completed_state)
            payloads.append(
                web._json_request(
                    base_url,
                    "POST",
                    web._session_path(web_session_id, "/messages"),
                    {"text": "请说明刚才部署的方案、总价和 Stack 结果。"},
                )
            )
            _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
        else:
            interrupt_message: dict[str, Any] = {"text": _web_replacement_intent_prompt(multimodal=False)}
            if runtime.spec.multimodal:
                upload = _upload_web_fixture(web, base_url, web_session_id, "rollback-interrupt")
                payloads.append(upload)
                interrupt_message = {
                    "text": _web_replacement_intent_prompt(multimodal=True),
                    "imageIds": [upload["imageId"]],
                }
            payloads.append(
                web._json_request(
                    base_url,
                    "POST",
                    web._session_path(web_session_id, "/messages"),
                    interrupt_message,
                )
            )
            # The old confirmation remains visible until this background turn
            # consumes it. Waiting for idle prevents the loop below from
            # mistaking that stale boundary for the result of the new intent
            # and posting a second action concurrently (HTTP 409).
            _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
            for index in range(10):
                recovered_state = _wait_web_state(
                    web,
                    base_url,
                    web_session_id,
                    lambda value: bool(_web_pending_kind(value)) or _contains_text(value, "pipeline_handoff_ready"),
                    runtime.args.stream_timeout,
                )
                payloads.append(recovered_state)
                kind = _web_pending_kind(recovered_state)
                if kind in {"candidate_selection", "candidate_select"}:
                    payloads.append(
                        _select_web_candidate(
                            web,
                            base_url,
                            model_session_id,
                            timeout=runtime.args.stream_timeout,
                        )
                    )
                    _wait_web_state(
                        web,
                        base_url,
                        web_session_id,
                        lambda value: _web_pending_kind(value) not in {"candidate_selection", "candidate_select"},
                        runtime.args.stream_timeout,
                    )
                elif kind == "ask_user_question":
                    payloads.append(
                        web._json_request(
                            base_url,
                            "POST",
                            web._session_path(web_session_id, "/messages"),
                            {"text": "使用低成本默认值继续。"},
                        )
                    )
                    _wait_web_state(
                        web,
                        base_url,
                        web_session_id,
                        lambda value: _web_pending_kind(value) != "ask_user_question",
                        runtime.args.stream_timeout,
                    )
                elif kind == "deployment_confirmation":
                    payloads.append(
                        web._json_request(
                            base_url,
                            "POST",
                            web._session_path(web_session_id, "/messages"),
                            {"text": _confirmation_payload("cancel")},
                        )
                    )
                    _wait_web_idle(web, base_url, web_session_id, runtime.args.stream_timeout)
                    runtime.checks["Web multimodal flow canceled at confirmation"] = True
                    break
                else:
                    break
            else:
                raise RuntimeError("Web multimodal rollback did not reach deployment confirmation")
        transcript = web._json_request(base_url, "GET", web._session_path(web_session_id, "/messages"))
        payloads.append(transcript)
        if not runtime.args.skip_browser:
            expected = str(initial_message["text"])
            web._verify_browser(
                base_url=base_url,
                session_id=web_session_id,
                expected_text=expected,
                screenshot=runtime.paths.artifacts_dir / "browser.png",
                dom_snapshot=runtime.paths.artifacts_dir / "browser-dom.txt",
                audit=runtime.paths.artifacts_dir / "browser-audit.json",
                require_quote=True,
                expand_pipeline_history=runtime.spec.case_id == "W02",
            )
            browser_audit = json.loads((runtime.paths.artifacts_dir / "browser-audit.json").read_text(encoding="utf-8"))
            required_browser_checks = (
                "expectedTextVisible",
                "solutionVisible",
                "quoteVisible",
                "previewSuccessHidden",
                "internalTemplatePathHidden",
                "internalParameterJsonHidden",
            )
            if runtime.spec.case_id == "W02":
                required_browser_checks += ("historyExpanded",)
            runtime.checks["real browser DOM rendered"] = all(
                browser_audit.get(name) is True for name in required_browser_checks
            )
    finally:
        web._stop_web_server(process, timeout=runtime.args.timeout)
    write_json(runtime.paths.artifacts_dir / "web-api-payloads.json", payloads)
    pipeline_events: list[Any] = []
    for path in sorted(runtime.paths.config_dir.glob("projects/*/*/a2a/pipeline/a2a-events.jsonl")):
        pipeline_events.extend(_read_json_lines(path))
    if not pipeline_events:
        raise RuntimeError("Web pipeline journal did not persist any events")
    web_event_path = runtime.paths.run_dir / "web-pipeline.events.jsonl"
    for event in pipeline_events:
        append_jsonl(web_event_path, event)
    _common_pipeline_checks(runtime, pipeline_events)
    text = _json_text(payloads)
    runtime.checks["Web shows solution information"] = any(marker in text for marker in ("方案", "solution"))
    if not runtime.args.skip_browser:
        runtime.checks["Web internal Preview detail hidden"] = browser_audit.get("previewSuccessHidden") is True


DESKTOP_RESULT_CHECKS = (
    "nativeHostStarted",
    "pythonSidecarStarted",
    "pipelineSelected",
    "candidateSelectionCompleted",
    "candidateWaitingRestartRecovered",
    "confirmationWaitingRestartRecovered",
    "directInputAdjusted",
    "canceled",
    "threeStepTimeline",
    "solutionSummaryVisible",
    "quoteVisible",
    "desktopRuntime",
    "normalHandoff",
)


def validate_desktop_result(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("Desktop E2E result must be a JSON object")
    steps = value.get("steps")
    package_resources = value.get("packageResources")
    return {
        "Desktop driver selected selling_solution_first": value.get("pipelineName") == PIPELINE_NAME,
        "Desktop driver observed exact three-step timeline": steps == list(NEW_STEPS),
        "Desktop driver completed native interaction contract": all(
            value.get(name) is True for name in DESKTOP_RESULT_CHECKS
        ),
        "Desktop driver canceled without cloud write": value.get("cloudWriteObserved") is False,
        "Desktop packaged pipeline resources audited": isinstance(package_resources, dict)
        and all(
            package_resources.get(name) is True
            for name in ("yaml", "prompts", "skills", "hooks", "tools", "references")
        ),
    }


def audit_desktop_source_resources(source_root: Path, required_resources: Sequence[str]) -> dict[str, Any]:
    """Audit required source files without relying on directory-symlink traversal.

    The solution-first skills intentionally reuse the selling reference tree via
    a directory symlink.  ``Path.rglob`` does not descend into such symlinks, so
    every declared resource must be checked directly.
    """

    present = [name for name in required_resources if (source_root / name).is_file()]
    missing = [name for name in required_resources if name not in present]
    return {
        "requiredResources": list(required_resources),
        "sourceResourcesPresent": present,
        "missingSourceResources": missing,
        "allPresent": not missing,
    }


def _run_desktop(runtime: ScenarioRuntime) -> None:
    required_source_resources = (
        "pipeline.yaml",
        "prompts/solution_planning_and_selection.md",
        "prompts/materialize_selected_candidate.md",
        "prompts/deploying.md",
        "hooks/deploying.py",
        "hooks/materialize_selected_candidate.py",
        "tools/confirmed_ros_deploy_tool.py",
        "tools/reused_selling_tools.py",
        "tools/show_architecture_plan_tool.py",
        "skills/iac-aliyun-solution-first/SKILL.md",
        "skills/iac-aliyun-materialize-selected-candidate/SKILL.md",
        "skills/iac-aliyun-deploying/SKILL.md",
        "skills/iac-aliyun-materialize-selected-candidate/references/ros-template.md",
    )
    source_root = REPO_ROOT / "src" / "iac_code" / "pipeline" / PIPELINE_NAME
    source_audit = audit_desktop_source_resources(source_root, required_source_resources)
    runtime.checks["Desktop package source contains pipeline resources"] = source_audit["allPresent"]
    package_root = Path(runtime.args.desktop_package_root).expanduser().resolve()
    package_audit: dict[str, Any] = {
        "sourceRoot": str(source_root),
        "packageRoot": str(package_root),
        "platform": sys.platform,
        **source_audit,
    }
    write_json(runtime.paths.artifacts_dir / "desktop-package-resource-audit.json", package_audit)
    command = runtime.args.desktop_command.strip()
    if not command:
        raise RuntimeError(
            "D01 requires --desktop-command pointing to a platform-native UI driver; "
            "a sidecar-only smoke cannot satisfy the Desktop interaction contract"
        )
    result_path = runtime.paths.artifacts_dir / "desktop-runtime-audit.json"
    screenshot_path = runtime.paths.artifacts_dir / "desktop.png"
    host_log_path = runtime.paths.logs_dir / "desktop-host.log"
    sidecar_log_path = runtime.paths.logs_dir / "desktop-sidecar.log"
    driver_log_path = runtime.paths.logs_dir / "desktop-driver.log"
    driver_env = runtime.env.copy()
    driver_env.update(
        {
            "IAC_CODE_DESKTOP_E2E_RESULT": str(result_path),
            "IAC_CODE_DESKTOP_E2E_SCREENSHOT": str(screenshot_path),
            "IAC_CODE_DESKTOP_E2E_HOST_LOG": str(host_log_path),
            "IAC_CODE_DESKTOP_E2E_SIDECAR_LOG": str(sidecar_log_path),
            "IAC_CODE_DESKTOP_E2E_PACKAGE_ROOT": str(package_root),
        }
    )
    completed = subprocess.run(
        shlex.split(command),
        cwd=REPO_ROOT / "desktop",
        env=driver_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=runtime.args.stream_timeout,
        check=False,
    )
    redacted_output = _legacy_repl_module()._redact_sensitive_text(completed.stdout + completed.stderr, runtime.env)
    driver_log_path.write_text(redacted_output, encoding="utf-8")
    runtime.checks["Desktop native driver exited successfully"] = completed.returncode == 0
    if not result_path.is_file():
        raise RuntimeError(f"Desktop native driver did not write required result manifest: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    runtime.checks.update(validate_desktop_result(result))
    package_audit["driverPackageResources"] = result.get("packageResources") if isinstance(result, dict) else None
    write_json(runtime.paths.artifacts_dir / "desktop-package-resource-audit.json", package_audit)
    runtime.checks["Desktop screenshot captured"] = screenshot_path.is_file()
    runtime.checks["Desktop host and sidecar logs captured"] = host_log_path.is_file() and sidecar_log_path.is_file()
    runtime.checks["Desktop runtime uses isolated config"] = runtime.env["IAC_CODE_CONFIG_DIR"] == str(
        runtime.paths.config_dir
    )


def _run_legacy(runtime: ScenarioRuntime) -> None:
    _run_a2a(runtime)
    values = _all_event_values(runtime.paths.run_dir)
    text = _json_text(values)
    legacy_steps = ("intent_parsing", "architecture_planning", "evaluate_candidates", "confirm_and_select", "deploying")
    manifest = yaml.safe_load((REPO_ROOT / "src/iac_code/pipeline/selling/pipeline.yaml").read_text(encoding="utf-8"))
    configured_steps = [str(item.get("id") or "") for item in manifest.get("steps", []) if isinstance(item, dict)]
    runtime.checks["legacy five-step definition retained"] = configured_steps == list(legacy_steps)
    observed = [step for _, step in _started_steps(values) if step in legacy_steps]
    first_observed = list(dict.fromkeys(observed))
    runtime.checks["legacy observed step order retained"] = first_observed == list(legacy_steps[: len(first_observed)])
    runtime.checks["legacy candidate alias retained"] = "candidate" in text.lower()
    runtime.checks["legacy smoke made no cloud write"] = "ros_deploy" not in {
        item["tool"] for item in _tool_sequence(values)
    } and not discover_cloud_resources(runtime)
    confirm_step = next(
        (
            item
            for item in manifest.get("steps", [])
            if isinstance(item, dict) and item.get("id") == "confirm_and_select"
        ),
        {},
    )
    properties = confirm_step.get("conclusion_schema", {}).get("properties", {})
    runtime.checks["legacy parameter schema retained"] = "parameter_overrides" in properties


def _mapping_string(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    return ""


def discover_cloud_resources(runtime: ScenarioRuntime) -> list[dict[str, str]]:
    values: list[Any] = _all_event_values(runtime.paths.run_dir)
    # REPL does not write A2A ``*.events.jsonl`` files. Its authoritative tool
    # inputs/results live in the persisted parent-step transcripts, including
    # the ros_deploy Stack ID needed for ownership-checked teardown.
    values.extend(_read_repl_transcript_values(runtime))
    for path in (runtime.paths.artifacts_dir / "web-api-payloads.json", runtime.paths.run_dir / "repl-events.jsonl"):
        if path.suffix == ".jsonl":
            values.extend(_read_json_lines(path))
        elif path.is_file():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                values.append(json.loads(path.read_text(encoding="utf-8")))
    resources: dict[str, dict[str, str]] = {}
    for value in values:
        candidates = [item for _, item in _walk(value) if isinstance(item, dict)]
        if isinstance(value, dict):
            candidates.append(value)
        for item in candidates:
            explicit_stack_id = _mapping_string(item, "stackId", "stack_id", "StackId")
            resource_id = _mapping_string(item, "resourceId", "resource_id")
            action = _mapping_string(item, "action", "Action", "apiName", "api_name")
            resource_type = _mapping_string(item, "resourceType", "resource_type", "type").lower()
            provider = _mapping_string(item, "provider", "Provider").lower()
            is_create = action in {"CreateStack", "ContinueCreateStack"}
            is_stack_resource = "stack" in resource_type and provider in {"", "ros", "aliyun"}
            stack_id = explicit_stack_id or (resource_id if is_stack_resource else "")
            if not stack_id or not re.fullmatch(r"[A-Za-z0-9_-]{6,}", stack_id):
                continue
            stack_name = _mapping_string(item, "stackName", "stack_name", "StackName", "resourceName", "resource_name")
            region_id = _mapping_string(item, "regionId", "region_id", "RegionId")
            if not is_create and not is_stack_resource and stack_name not in runtime.owned_stack_names:
                continue
            previous = resources.setdefault(
                stack_id,
                {
                    "provider": "ros",
                    "resourceType": "stack",
                    "stackId": stack_id,
                    "stackName": "",
                    "regionId": "",
                    "createdByCase": "true" if is_create else "false",
                },
            )
            previous["stackName"] = previous["stackName"] or stack_name
            previous["regionId"] = previous["regionId"] or region_id
            if is_create:
                previous["createdByCase"] = "true"
    result = [
        item
        for item in resources.values()
        if item["createdByCase"] == "true" or item["stackName"] in runtime.owned_stack_names
    ]
    runtime.cloud_resources = result
    write_json(runtime.paths.run_dir / "cloud-resources.json", result)
    return result


_CLOUD_CLEANUP_CODE = r"""
import json, sys, time
from alibabacloud_ros20190910 import models as ros_models
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

item = json.load(open(sys.argv[1], encoding="utf-8"))
credential = CloudCredentials().get_provider("aliyun")
if credential is None:
    raise RuntimeError("Aliyun credential is unavailable")
region = item.get("regionId") or credential.region_id
client = RosClientFactory.create(credential, region)
stack_id = item["stackId"]
expected = item["stackName"]
request = ros_models.GetStackRequest(stack_id=stack_id, region_id=region)
deadline = time.monotonic() + 900
while time.monotonic() < deadline:
    try:
        actual = client.get_stack(request).body.to_map()
    except Exception as exc:
        if "not found" in str(exc).lower() or "stacknotfound" in str(exc).lower():
            print(json.dumps({"deleted": True, "notFound": True}))
            raise SystemExit(0)
        raise
    if actual.get("StackName") != expected:
        raise RuntimeError("Stack ownership mismatch; refusing delete")
    status = actual.get("Status", "")
    if status == "DELETE_COMPLETE":
        print(json.dumps({"deleted": True, "status": status}))
        raise SystemExit(0)
    if status == "DELETE_IN_PROGRESS" or (isinstance(status, str) and status.endswith("_IN_PROGRESS")):
        time.sleep(5)
        continue
    try:
        client.delete_stack(ros_models.DeleteStackRequest(stack_id=stack_id, region_id=region))
    except Exception as exc:
        message = str(exc).lower()
        if "actioninprogress" not in message and "action in progress" not in message:
            raise
    time.sleep(5)
raise TimeoutError("timed out waiting for ROS Stack deletion")
"""


def cleanup_cloud_resources(runtime: ScenarioRuntime) -> str:
    resources = discover_cloud_resources(runtime)
    if runtime.args.skip_final_teardown:
        payload = {"status": "skipped", "reason": "--skip-final-teardown", "resources": resources}
        write_json(runtime.paths.run_dir / "cleanup-result.json", payload)
        return "skipped"
    failures: list[str] = []
    deleted: list[str] = []
    for resource in resources:
        stack_id = resource.get("stackId", "")
        stack_name = resource.get("stackName", "")
        if not stack_id:
            continue
        # The Stack ID and the exact test-owned name are both mandatory before deletion.
        if (
            not stack_name
            or stack_name not in runtime.owned_stack_names
            or not stack_name.startswith(STACK_PREFIX + "-")
        ):
            failures.append(f"{stack_id}: ownership could not be proven with the exact test StackName")
            continue
        manifest = runtime.paths.artifacts_dir / f"cleanup-{stack_id}.json"
        write_json(manifest, resource)
        completed = subprocess.run(
            [*shlex.split(runtime.args.python), "-c", _CLOUD_CLEANUP_CODE, str(manifest)],
            cwd=REPO_ROOT,
            env=runtime.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=runtime.args.stream_timeout,
            check=False,
        )
        if completed.returncode == 0:
            deleted.append(stack_id)
        else:
            failures.append(f"{stack_id}: cleanup subprocess exited {completed.returncode}")
        (runtime.paths.logs_dir / f"cleanup-{stack_id}.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
    status_value = "failed" if failures else "completed"
    write_json(
        runtime.paths.run_dir / "cleanup-result.json",
        {"status": status_value, "deletedStackIds": deleted, "failures": failures, "resources": resources},
    )
    runtime.checks["test-owned stacks cleaned"] = not failures
    return status_value


def collect_templates(runtime: ScenarioRuntime) -> int:
    count = 0
    for root in (runtime.paths.workspace_dir, runtime.paths.config_dir / "projects"):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml", ".json", ".tf"}:
                continue
            if path.name in {"settings.yml", *CREDENTIAL_FILES}:
                continue
            if not _is_iac_template_file(path):
                continue
            target = runtime.paths.templates_dir / f"{count:03d}-{path.name}"
            shutil.copy2(path, target)
            count += 1
    return count


def _is_iac_template_file(path: Path) -> bool:
    if path.suffix.lower() == ".tf":
        return True
    try:
        # ROS templates legitimately use short-form intrinsic tags such as
        # ``!GetAtt``. BaseLoader preserves the mapping shape without trying to
        # construct those application-specific values.
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return False
    if not isinstance(value, dict):
        return False
    return (
        "ROSTemplateFormatVersion" in value
        or "AWSTemplateFormatVersion" in value
        or (isinstance(value.get("Resources"), dict) and bool(value["Resources"]))
    )


def _event_type_count(values: Sequence[Any], event_type: str) -> int:
    count = 0
    for value in values:
        candidates = [item for _, item in _walk(value) if isinstance(item, dict)]
        if isinstance(value, dict):
            candidates.append(value)
        count += sum(item.get("eventType") == event_type or item.get("event_type") == event_type for item in candidates)
    return count


def _copied_credential_values(runtime: ScenarioRuntime) -> list[str]:
    values: list[str] = []

    def collect(value: Any, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                upper = str(key).upper()
                collect(item, sensitive or any(marker in upper for marker in ("KEY", "SECRET", "TOKEN", "PASSWORD")))
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, str) and len(value) >= 6:
            values.append(value)

    for name in CREDENTIAL_FILES:
        path = runtime.paths.config_dir / name
        if not path.is_file():
            continue
        with contextlib.suppress(OSError, ValueError):
            collect(yaml.safe_load(path.read_text(encoding="utf-8")))
    return values


def credential_values_absent_from_artifacts(runtime: ScenarioRuntime) -> bool:
    sensitive_values = set(_copied_credential_values(runtime))
    if not sensitive_values:
        return True
    excluded_roots = (
        runtime.paths.config_dir.resolve(),
        runtime.paths.backup_dir.resolve(),
        (runtime.paths.run_dir / ".preflight" / "config").resolve(),
        (runtime.paths.run_dir / ".preflight" / "config-backup").resolve(),
    )
    text_suffixes = {".json", ".jsonl", ".log", ".txt", ".yaml", ".yml", ".md"}
    for path in runtime.paths.run_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        resolved = path.resolve()
        if any(resolved == root or is_relative_to(resolved, root) for root in excluded_roots):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(value in content for value in sensitive_values):
            return False
    return True


def _noecho_parameter_names(runtime: ScenarioRuntime) -> set[str]:
    from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

    names: set[str] = set()
    roots = (runtime.paths.workspace_dir, runtime.paths.run_dir / "templates")
    for root in roots:
        if not root.is_dir():
            continue
        for path in (*root.rglob("*.yml"), *root.rglob("*.yaml")):
            with contextlib.suppress(OSError, UnicodeError, yaml.YAMLError):
                template = ros_yaml_load(path.read_text(encoding="utf-8"))
                declarations = template.get("Parameters") if isinstance(template, dict) else None
                if not isinstance(declarations, dict):
                    continue
                for name, declaration in declarations.items():
                    noecho = declaration.get("NoEcho") if isinstance(declaration, dict) else None
                    if isinstance(name, str) and (
                        noecho is True or (isinstance(noecho, str) and noecho.strip().lower() == "true")
                    ):
                        names.add(name)
    return names


def _public_noecho_values_are_redacted(runtime: ScenarioRuntime, values: list[Any]) -> bool:
    names = _noecho_parameter_names(runtime)
    if not names:
        return False
    observed = False
    for _, item in _walk(values):
        if not isinstance(item, dict):
            continue
        candidates: list[Any] = []
        for name in names:
            if name in item and not isinstance(item[name], (dict, list)):
                candidates.append(item[name])
        if item.get("parameter_name") in names and "actual_value" in item:
            candidates.append(item["actual_value"])
        for value in candidates:
            observed = True
            normalized = str(value or "").strip().lower()
            if not (
                normalized in {"", "***", "******", "<redacted>", "[redacted]"}
                or (normalized and set(normalized) == {"*"})
            ):
                return False
    return observed


def run_public_contract_audit(runtime: ScenarioRuntime) -> None:
    contract = importlib.import_module("scripts.aliyun.e2e_contract_audit")
    values = _all_event_values(runtime.paths.run_dir)
    web_payload_path = runtime.paths.artifacts_dir / "web-api-payloads.json"
    if web_payload_path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            values.append(json.loads(web_payload_path.read_text(encoding="utf-8")))
    forbidden_values = _copied_credential_values(runtime)
    try:
        persisted_path, tool_result = contract.find_latest_aliyun_tool_result(runtime.paths.config_dir)
        content = tool_result.get("content")
        metadata = tool_result.get("metadata")
        if not isinstance(content, str) or not isinstance(metadata, dict):
            raise AssertionError("persisted aliyun_api ToolResult has an invalid content/metadata shape")
        try:
            expected_body: Any = json.loads(content)
        except json.JSONDecodeError:
            expected_body = content
        result = contract.audit_aliyun_result_contract(
            expected_body=expected_body,
            tool_result_content=content,
            tool_result_metadata=metadata,
            public_payloads=values,
            forbidden_values=forbidden_values,
            output_path=runtime.paths.artifacts_dir / "aliyun-business-body-audit.json",
        )
        runtime.checks["Aliyun business body and public payload contract passed"] = result["passed"] is True
        runtime.checks["persisted Aliyun tool result belongs to isolated config"] = is_relative_to(
            persisted_path.resolve(), runtime.paths.config_dir.resolve()
        )
    except (AssertionError, OSError, ValueError) as exc:
        write_json(
            runtime.paths.artifacts_dir / "aliyun-business-body-audit.json",
            {"passed": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        runtime.checks["Aliyun business body and public payload contract passed"] = False
    tools = [item["tool"].lower() for item in _tool_sequence(values)]
    runtime.checks["public events preserve Aliyun tool attribution"] = "aliyun_api" in tools
    if runtime.spec.case_id in {"A01", "W01"}:
        runtime.checks["deployed flow preserves ros_deploy attribution"] = "ros_deploy" in tools


def _repl_step1_clarification_checks(
    repl_events: list[dict[str, Any]], display_events: list[dict[str, Any]]
) -> tuple[bool, bool]:
    question_index = next(
        (
            index
            for index, item in enumerate(repl_events)
            if item.get("type") == "expect" and "question input ready" in str(item.get("description") or "")
        ),
        -1,
    )
    selection_indexes = [
        index
        for index, item in enumerate(repl_events)
        if item.get("type") == "display-event" and item.get("event_type") == "candidate_selection_ready"
    ]
    interrupt_index = next(
        (index for index, item in enumerate(repl_events) if item.get("type") == "candidate-interrupt"),
        -1,
    )
    clarification_preceded_selection = (
        question_index >= 0 and bool(selection_indexes) and question_index < selection_indexes[0]
    )
    interaction_replanned = (
        len(selection_indexes) >= 2 and selection_indexes[0] < interrupt_index < selection_indexes[1]
    )

    step1_starts = [
        index
        for index, item in enumerate(display_events)
        if item.get("type") == "step_started" and item.get("step_id") == NEW_STEPS[0]
    ]
    selection_ready = [
        index
        for index, item in enumerate(display_events)
        if item.get("type") == "candidate_selection_ready" and item.get("step_id") == NEW_STEPS[0]
    ]
    rerendered = False
    if len(step1_starts) >= 2 and len(selection_ready) >= 2 and step1_starts[1] < selection_ready[1]:
        rerendered_types = {item.get("type") for item in display_events[step1_starts[1] + 1 : selection_ready[1]]}
        rerendered = {"candidate_diagram", "candidate_detail"}.issubset(rerendered_types)
    return clarification_preceded_selection, interaction_replanned and rerendered


def _repl_progress_follows_step_order(display_events: list[dict[str, Any]], *, require_all: bool) -> bool:
    observed = [step for _, step in _started_steps(display_events) if step in NEW_STEPS]
    if not observed or observed[0] != NEW_STEPS[0]:
        return False
    indexes = [NEW_STEPS.index(step) for step in observed]
    transitions_valid = all(
        current == previous or current == previous + 1 or current == 0
        for previous, current in zip(indexes, indexes[1:])
    )
    return transitions_valid and (not require_all or set(observed) == set(NEW_STEPS))


def _read_repl_transcript_values(runtime: ScenarioRuntime) -> list[Any]:
    values: list[Any] = []
    config_dir = getattr(runtime.paths, "config_dir", None)
    if not isinstance(config_dir, (str, os.PathLike)):
        return values
    for path in sorted((Path(config_dir) / "projects").glob("*/*/pipeline/transcripts/*/session.jsonl")):
        values.extend(_read_json_lines(path))
    return values


def _repl_natural_adjustment_checks(
    display_events: list[dict[str, Any]], transcript_values: list[Any]
) -> dict[str, bool]:
    step2_inputs = [
        (index, item.get("payload", {}))
        for index, item in enumerate(display_events)
        if item.get("type") == "user_input_received"
        and item.get("step_id") == NEW_STEPS[1]
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("structured") is False
    ]
    confirmations = [
        (index, item.get("payload", {}))
        for index, item in enumerate(display_events)
        if item.get("type") == "user_input_required"
        and item.get("step_id") == NEW_STEPS[1]
        and isinstance(item.get("payload"), dict)
    ]
    step3_starts = [
        index
        for index, item in enumerate(display_events)
        if item.get("type") == "step_started" and item.get("step_id") == NEW_STEPS[2]
    ]
    adjustment_input_index = step2_inputs[0][0] if step2_inputs else -1
    confirmation_input_index = step2_inputs[1][0] if len(step2_inputs) >= 2 else -1
    refreshed_after_adjustment = any(index > adjustment_input_index for index, _ in confirmations[1:])
    deployed_after_natural_confirmation = any(index > confirmation_input_index for index in step3_starts)

    first_confirmation = confirmations[0][1] if confirmations else {}
    latest_confirmation = confirmations[-1][1] if len(confirmations) >= 2 else {}
    refreshed_content = len(confirmations) >= 2 and (
        first_confirmation.get("solution_summary") != latest_confirmation.get("solution_summary")
        or first_confirmation.get("effective_deployment_parameters")
        != latest_confirmation.get("effective_deployment_parameters")
    )
    exact_tool_names = [
        item
        for key, item in _walk(transcript_values)
        if key in {"name", "tool_name", "toolName"} and item in {"ros_preview_template", "ros_estimate_template_cost"}
    ]
    return {
        "REPL direct text produced an adjustment": adjustment_input_index >= 0 and refreshed_after_adjustment,
        "REPL natural language confirmation was classified": (
            confirmation_input_index > adjustment_input_index and deployed_after_natural_confirmation
        ),
        "REPL adjustment produced a refreshed confirmation": refreshed_content,
        "REPL adjustment reran Preview and quote": (
            exact_tool_names.count("ros_preview_template") >= 2
            and exact_tool_names.count("ros_estimate_template_cost") >= 2
        ),
    }


def apply_profile_acceptance(runtime: ScenarioRuntime) -> None:
    spec = runtime.spec
    values = _all_event_values(runtime.paths.run_dir)
    runtime_events = _read_json_lines(runtime.events_path)
    text = _json_text(values)
    waiting_path = runtime.paths.artifacts_dir / "waiting-sequence.json"
    waiting: list[str] = []
    if waiting_path.is_file():
        with contextlib.suppress(json.JSONDecodeError):
            loaded = json.loads(waiting_path.read_text(encoding="utf-8"))
            waiting = [str(item) for item in loaded] if isinstance(loaded, list) else []
    if spec.surface in {Surface.A2A, Surface.LEGACY}:
        runtime.checks["A2A final task snapshot captured"] = (
            any(runtime.paths.run_dir.glob("final-task-*.json"))
            or (runtime.paths.snapshots_dir / "final.json").is_file()
        )
    profile = spec.profile
    if profile == "step1_clarify":
        if spec.surface is Surface.REPL:
            clarification_preceded_selection, candidate_edit_replanned = _repl_step1_clarification_checks(
                _read_json_lines(runtime.paths.run_dir / "repl-events.jsonl"),
                _read_repl_display_events(runtime),
            )
            runtime.checks["Step 1 clarification preceded selection"] = clarification_preceded_selection
            runtime.checks["REPL candidate edit reran Step 1 diagram and detail"] = candidate_edit_replanned
        else:
            runtime.checks["Step 1 clarification preceded selection"] = bool(waiting) and (
                waiting[0].endswith(":ask_user_question")
                and any(item.startswith(NEW_STEPS[0] + ":candidate") for item in waiting[1:])
            )
    elif profile == "replace_invalid":
        repl_events = _read_json_lines(runtime.paths.run_dir / "repl-events.jsonl")
        display_events = _read_repl_display_events(runtime)
        invalid_index = next(
            (index for index, event in enumerate(repl_events) if event.get("type") == "candidate-invalid"),
            -1,
        )
        replacement_index = next(
            (
                index
                for index, event in enumerate(repl_events)
                if event.get("type") == "candidate-interrupt-input"
                and "我改需求了：只创建一个安全组" in str(event.get("text", ""))
            ),
            -1,
        )
        step1_starts = [
            index
            for index, event in enumerate(display_events)
            if event.get("type") == "step_started" and event.get("step_id") == NEW_STEPS[0]
        ]
        replacement_details = [
            event
            for index, event in enumerate(display_events)
            if len(step1_starts) >= 2
            and index > step1_starts[-1]
            and event.get("type") == "candidate_detail"
            and event.get("step_id") == NEW_STEPS[0]
        ]
        runtime.checks["REPL invalid candidate preceded replacement intent"] = (
            invalid_index >= 0 and replacement_index > invalid_index
        )
        runtime.checks["REPL replacement reran Step 1 and produced selectable candidates"] = (
            len(step1_starts) >= 2
            and sum(event.get("type") == "candidate_selection_ready" for event in display_events) >= 2
            and any(event.get("type") == "candidate_selected" for event in display_events)
        )
        runtime.checks["REPL replacement candidate reflects the new security-group target"] = bool(
            replacement_details
        ) and "安全组" in _json_text(replacement_details)
    elif profile == "step1_replace":
        runtime.checks["Step 1 replanned and replaced intent"] = _event_type_count(values, "input_received") >= 3
    elif profile == "step2_parameter":
        if spec.surface is Surface.REPL:
            repl_events = _read_json_lines(runtime.paths.run_dir / "repl-events.jsonl")
            display_events = _read_repl_display_events(runtime)
            candidate_index = next(
                (index for index, event in enumerate(repl_events) if event.get("type") == "candidate-enter"),
                -1,
            )
            parameter_questions = [
                index
                for index, event in enumerate(repl_events)
                if event.get("type") == "expect"
                and str(event.get("description", "")).startswith("Step 2 ")
                and str(event.get("description", "")).endswith("parameter question input ready")
            ]
            runtime.checks["deployment parameters were requested only after Step 2 started"] = (
                any(
                    event.get("type") == "step_started" and event.get("step_id") == NEW_STEPS[1]
                    for event in display_events
                )
                and len(parameter_questions) >= 2
                and all(index > candidate_index for index in parameter_questions)
                and not any(
                    event.get("type") == "expect" and str(event.get("description", "")).startswith("pipeline question")
                    for event in repl_events
                )
            )
        else:
            runtime.checks["deployment parameter was requested only in Step 2"] = any(
                item == f"{NEW_STEPS[1]}:ask_user_question" for item in waiting
            ) and not any(item == f"{NEW_STEPS[0]}:ask_user_question" for item in waiting)
    elif profile == "structured_override":
        runtime.checks["structured override caused a second confirmation"] = (
            sum(item.endswith(":deployment_confirmation") for item in waiting) >= 2
        )
        runtime.checks["candidate payload override was not authoritative"] = (
            "10.99.99.0/24" not in text or runtime.cidr in text
        )
    elif profile == "natural_adjust":
        display_events = _read_repl_display_events(runtime)
        runtime.checks.update(_repl_natural_adjustment_checks(display_events, _read_repl_transcript_values(runtime)))
    elif profile == "reselect_new_intent":
        runtime.checks["reselect and new intent both returned to Step 1"] = (
            sum(step == NEW_STEPS[0] for _, step in _started_steps(values)) >= 3
        )
    elif profile == "early_exit":
        runtime.checks["non-Aliyun request generated no IaC artifact"] = not any(
            marker in text for marker in ("PreviewStack", "GetTemplateEstimateCost", "ros_deploy")
        )
    elif profile == "input_during_backup":
        evidence_path = runtime.paths.artifacts_dir / "backup-input-checkpoints.json"
        evidence: Any = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        checkpoints = evidence.get("checkpoints") if isinstance(evidence, dict) else None
        runtime.checks["four backup-window inputs were consumed as pending input"] = (
            isinstance(checkpoints, list)
            and len(checkpoints) == 4
            and all(
                isinstance(item, dict)
                and item.get("requestDispatchedDuringBackup") is True
                and item.get("consumedAsPendingInput") is True
                and item.get("classifiedAsInterrupt") is False
                for item in checkpoints
            )
        )
    elif profile == "fault_checkpoints":
        restarted = sum(item.get("type") == "server-restarted" for item in runtime_events if isinstance(item, dict))
        runtime.checks["all six fault checkpoints restarted"] = restarted >= 6
    elif profile.startswith("rollback"):
        runtime.checks["rollback restarted Step 1"] = (
            sum(step == NEW_STEPS[0] for _, step in _started_steps(values)) >= 2
        )
        if profile in {"rollback_cleanup", "rollback_cleanup_recovery"}:
            resources = discover_cloud_resources(runtime)
            runtime.checks["rollback cleanup observed two distinct Stacks"] = (
                len({item.get("stackId") for item in resources if item.get("stackId")}) >= 2
            )
            runtime.checks["rollback cleanup emitted cleanup evidence"] = any(
                marker in text for marker in ("cleanup_started", "cleanup_completed", "DELETE_COMPLETE")
            )
            if profile == "rollback_cleanup_recovery":
                runtime.checks["rollback cleanup restarted after cleanup began"] = any(
                    item.get("checkpoint") == "rollback-cleanup-started"
                    for item in runtime_events
                    if isinstance(item, dict)
                )
    elif profile == "redaction":
        secret_values = _copied_credential_values(runtime)
        runtime.checks["credential values absent from A2A payload"] = all(value not in text for value in secret_values)
        runtime.checks["NoEcho parameter values absent from A2A payload"] = _public_noecho_values_are_redacted(
            runtime, values
        )
        runtime.checks["functional price and parameters not over-redacted"] = any(
            marker in text for marker in ("OriginalAmount", "TradeAmount", "费用明细")
        ) and any(marker in text for marker in ("NoEcho", "parameter", "参数"))
    if spec.multimodal:
        runtime.checks["multimodal fixture evidence captured"] = any(
            path.is_file()
            for path in (
                runtime.paths.run_dir / "image-fixtures" / "manifest.json",
                runtime.paths.run_dir / "repl-events.jsonl",
                runtime.paths.artifacts_dir / "web-api-payloads.json",
            )
        )
    if spec.surface is Surface.REPL:
        transcript_path = runtime.paths.run_dir / "transcript.normalized.log"
        transcript = transcript_path.read_text(encoding="utf-8", errors="replace") if transcript_path.is_file() else ""
        display_events = _read_repl_display_events(runtime)
        runtime.checks["REPL progress follows three-step state machine"] = _repl_progress_follows_step_order(
            display_events,
            require_all=spec.cloud_write,
        )
        if "询价概览" in transcript:
            runtime.checks["REPL confirmation focuses solution and quote"] = all(
                marker in transcript for marker in ("方案说明", "询价概览", "费用明细")
            )
    if spec.surface is Surface.WEB:
        runtime.checks["Web API payload artifact captured"] = (
            runtime.paths.artifacts_dir / "web-api-payloads.json"
        ).is_file()


def _dispatch_surface(runtime: ScenarioRuntime) -> None:
    if runtime.spec.surface is Surface.A2A:
        _run_a2a(runtime)
    elif runtime.spec.surface is Surface.REPL:
        _run_repl(runtime)
    elif runtime.spec.surface is Surface.WEB:
        _run_web(runtime)
    elif runtime.spec.surface is Surface.DESKTOP:
        _run_desktop(runtime)
    elif runtime.spec.surface is Surface.LEGACY:
        _run_legacy(runtime)
    else:  # pragma: no cover - exhaustive enum guard
        raise AssertionError(runtime.spec.surface)


def _write_case_summary(runtime: ScenarioRuntime, result: ScenarioResult) -> None:
    write_json(runtime.paths.run_dir / "summary.json", dataclasses.asdict(result))
    write_json(runtime.paths.run_dir / "cloud-resources.json", runtime.cloud_resources)
    if not (runtime.paths.run_dir / "cleanup-result.json").exists():
        write_json(runtime.paths.run_dir / "cleanup-result.json", {"status": result.cleanup_status})


def run_one_scenario(
    spec: ScenarioSpec,
    args: argparse.Namespace,
    services: RunnerServices,
    runtime_defaults: Mapping[str, str],
    suite_root: Path,
) -> ScenarioResult:
    started_wall = utc_now()
    started = time.monotonic()
    runtime: ScenarioRuntime | None = None
    error = ""
    cleanup_status = "not-needed"
    status_value = "failed"
    try:
        runtime = create_runtime(spec, args, services, runtime_defaults)
        services.register_runtime(runtime)
        append_jsonl(
            suite_root / "suite-events.jsonl",
            {"at": utc_now(), "type": "case-started", "caseId": spec.case_id, "scenario": spec.name},
            services.suite_event_lock,
        )
        if services.cancel_event.is_set():
            raise InterruptedError("suite cancellation requested before case start")
        observe_module = importlib.import_module("scripts.observability.local_observe.e2e_audit")
        observe = observe_module.ObserveCapture(runtime.paths.artifacts_dir / "telemetry").start()
        runtime.env.update(observe.env)
        try:
            with services.locks.acquire(spec.resource_lock):
                _dispatch_surface(runtime)
        finally:
            telemetry_records = observe.stop()
        if spec.surface is not Surface.DESKTOP:
            runtime.checks["real telemetry captured"] = bool(telemetry_records)
        if spec.case_id in {"A01", "A24", "W01"}:
            telemetry_audit = observe_module.audit_provider_attempts(
                telemetry_records,
                output_path=runtime.paths.artifacts_dir / "provider-telemetry-audit.json",
            )
            runtime.checks["provider telemetry has unique terminal records"] = telemetry_audit["passed"]
            run_public_contract_audit(runtime)
        collect_templates(runtime)
        if not (runtime.paths.run_dir / "tool-sequence.json").is_file():
            write_json(runtime.paths.run_dir / "tool-sequence.json", [])
        apply_profile_acceptance(runtime)
        cleanup_status = cleanup_cloud_resources(runtime)
        runtime.checks["no child process remains"] = runtime.terminate_processes()
        runtime.checks["credential values absent from case artifacts"] = credential_values_absent_from_artifacts(
            runtime
        )
        status_value = "passed" if all(runtime.checks.values()) and cleanup_status != "failed" else "failed"
    except InterruptedError as exc:
        error = str(exc)
        status_value = "canceled"
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        status_value = "failed"
    if runtime is None:
        # Failure before runtime construction still receives a durable case directory.
        run_dir = case_run_dir(Path(args.run_root), spec, args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        checks: dict[str, bool] = {}
        notes = []
    else:
        processes_clean = runtime.terminate_processes()
        runtime.checks["no child process remains"] = processes_clean
        if cleanup_status == "not-needed":
            try:
                cleanup_status = cleanup_cloud_resources(runtime)
            except BaseException as cleanup_exc:
                cleanup_status = "failed"
                runtime.notes.append(f"cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if error:
            runtime.notes.append(error)
        run_dir = runtime.paths.run_dir
        checks = runtime.checks
        notes = runtime.notes
    if status_value not in {"canceled", "not-started"}:
        status_value = (
            "passed"
            if runtime is not None and not error and all(checks.values()) and cleanup_status != "failed"
            else "failed"
        )
    result = ScenarioResult(
        case_id=spec.case_id,
        scenario=spec.name,
        surface=spec.surface.value,
        status=status_value,
        started_at=started_wall,
        finished_at=utc_now(),
        duration_seconds=round(time.monotonic() - started, 3),
        run_dir=str(run_dir),
        checks=checks,
        notes=notes,
        cleanup_status=cleanup_status,
        error=error,
    )
    if runtime is not None:
        services.unregister_runtime(runtime)
        _write_case_summary(runtime, result)
    else:
        write_json(run_dir / "summary.json", dataclasses.asdict(result))
    append_jsonl(
        suite_root / "suite-events.jsonl",
        {
            "at": utc_now(),
            "type": "case-finished",
            "caseId": spec.case_id,
            "scenario": spec.name,
            "status": status_value,
            "runDir": str(run_dir),
        },
        services.suite_event_lock,
    )
    return result


def run_preflight(
    args: argparse.Namespace,
    suite_root: Path,
    source_dir: Path,
    runtime_defaults: Mapping[str, str],
    selected: Sequence[ScenarioSpec],
) -> dict[str, Any]:
    preflight_dir = suite_root / ".preflight"
    config_dir = preflight_dir / "config"
    backup_dir = preflight_dir / "config-backup"
    workspace = preflight_dir / "workspace"
    backup_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    audit = copy_credentials(source_dir, config_dir, inherit_settings=args.inherit_settings)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_CONFIG_BACKUP_DIR": str(backup_dir),
            "IAC_CODE_MODE": "normal",
        }
    )
    provider = args.provider or runtime_defaults.get("provider", "")
    model = args.model or runtime_defaults.get("model", "") or DEFAULT_TEXT_MODEL
    api_base = args.api_base or runtime_defaults.get("api_base", "")
    if provider:
        env["IAC_CODE_PROVIDER"] = provider
    if model:
        env["IAC_CODE_MODEL"] = model
    if api_base:
        env["IAC_CODE_BASE_URL"] = api_base
    if not audit.credential_files_copied:
        result = {"ok": False, "reason": "credential files are missing", "credentialFilesCopied": False}
        write_json(preflight_dir / "preflight.json", result)
        return result
    browser_required = any(spec.surface is Surface.WEB for spec in selected) and not args.skip_browser
    browser = (
        _run_browser_dependency_preflight(timeout=args.preflight_timeout)
        if browser_required
        else {"ok": True, "skipped": True}
    )
    common = importlib.import_module("scripts.a2a.e2e.common")
    llm = common.run_llm_preflight(
        python_cmd=shlex.split(args.python),
        cwd=str(REPO_ROOT),
        env=env,
        timeout=args.preflight_timeout,
        run_dir=preflight_dir,
    )
    # Real read-only cloud capability check. It lists at most one stack and does not
    # create, update, or delete any resource.
    cloud_code = r"""
from alibabacloud_ros20190910 import models as ros_models
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
credential = CloudCredentials().get_provider("aliyun")
if credential is None:
    raise RuntimeError("Aliyun credential is unavailable")
client = RosClientFactory.create(credential, credential.region_id)
client.list_stacks(ros_models.ListStacksRequest(region_id=credential.region_id, page_size=1))
print("ROS_READ_ONLY_OK")
"""
    cloud = subprocess.run(
        [*shlex.split(args.python), "-c", cloud_code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=args.preflight_timeout,
        check=False,
    )
    occupied_cidrs: list[str] = []
    cidr_query_ok = True
    if args.cleanup_vpc_id:
        cidr_code = r"""
import json, sys
from scripts.repl.e2e.run_pipeline_scenarios import _call_aliyun_api, _nested_api_items
data = _call_aliyun_api("vpc", "DescribeVSwitches", {"VpcId": sys.argv[1], "PageSize": 50})
items = _nested_api_items(data, "VSwitches", "VSwitch")
print(json.dumps([str(item.get("CidrBlock") or "") for item in items if item.get("CidrBlock")]))
"""
        cidr_query = subprocess.run(
            [*shlex.split(args.python), "-c", cidr_code, args.cleanup_vpc_id],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.preflight_timeout,
            check=False,
        )
        cidr_query_ok = cidr_query.returncode == 0
        if cidr_query_ok:
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(cidr_query.stdout.splitlines()[-1])
                if isinstance(parsed, list):
                    occupied_cidrs = [str(item) for item in parsed if isinstance(item, str)]
    result = {
        "ok": llm.get("ok") is True and cloud.returncode == 0 and cidr_query_ok and browser.get("ok") is True,
        "llmOk": llm.get("ok") is True,
        "cloudReadOnlyOk": cloud.returncode == 0,
        "credentialFilesCopied": audit.credential_files_copied,
        "cloudSummary": "ROS_READ_ONLY_OK" if cloud.returncode == 0 else f"exit code {cloud.returncode}",
        "vSwitchCidrQueryOk": cidr_query_ok,
        "occupiedVSwitchCidrs": occupied_cidrs,
        "browserRuntimeOk": browser.get("ok") is True,
        "browserRuntimeReason": browser.get("reason", ""),
    }
    write_json(preflight_dir / "suite-preflight.json", result)
    return result


def _run_browser_dependency_preflight(*, timeout: float) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        return {"ok": False, "reason": "Node.js is unavailable"}
    probe = r"""
const { createRequire } = require("node:module");
const fs = require("node:fs");
const path = require("node:path");
const requireFromProbe = createRequire(path.join(process.cwd(), "playwright-probe.cjs"));
const candidates = process.argv.slice(1);
try {
  requireFromProbe("playwright-core");
} catch (originalError) {
  const installed = candidates.find((candidate) => fs.existsSync(candidate));
  if (!installed) throw originalError;
  requireFromProbe(installed);
}
process.stdout.write("PLAYWRIGHT_CORE_OK\n");
"""
    candidate_roots = {
        REPO_ROOT / "node_modules" / "playwright-core",
        Path(tempfile.gettempdir()) / "iac-code-web-smoke-node" / "node_modules" / "playwright-core",
        Path("/tmp") / "iac-code-web-smoke-node" / "node_modules" / "playwright-core",
    }
    try:
        completed = subprocess.run(
            [node, "-e", probe, *(str(path) for path in sorted(candidate_roots))],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "reason": f"browser dependency probe failed: {type(exc).__name__}"}
    if completed.returncode != 0:
        return {
            "ok": False,
            "reason": (
                "playwright-core is unavailable; install it outside the repository under "
                f"{Path(tempfile.gettempdir()) / 'iac-code-web-smoke-node'}"
            ),
        }
    return {"ok": True, "reason": "PLAYWRIGHT_CORE_OK"}


def execute_selected(
    selected: Sequence[ScenarioSpec],
    args: argparse.Namespace,
    services: RunnerServices,
    runtime_defaults: Mapping[str, str],
    suite_root: Path,
    run_one: Callable[
        [ScenarioSpec, argparse.Namespace, RunnerServices, Mapping[str, str], Path], ScenarioResult
    ] = run_one_scenario,
) -> list[ScenarioResult]:
    results: dict[str, ScenarioResult] = {}
    pending_specs = iter(selected)
    in_flight: dict[concurrent.futures.Future[ScenarioResult], ScenarioSpec] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="ssf-e2e") as executor:

        def submit_available() -> None:
            while len(in_flight) < args.concurrency and not services.cancel_event.is_set():
                try:
                    spec = next(pending_specs)
                except StopIteration:
                    return
                future = executor.submit(run_one, spec, args, services, runtime_defaults, suite_root)
                in_flight[future] = spec

        submit_available()
        while in_flight:
            done, _ = concurrent.futures.wait(tuple(in_flight), return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                spec = in_flight.pop(future)
                try:
                    result = future.result()
                except BaseException as exc:  # pragma: no cover - run_one normally captures all failures
                    result = ScenarioResult(
                        spec.case_id,
                        spec.name,
                        spec.surface.value,
                        "failed",
                        utc_now(),
                        utc_now(),
                        0.0,
                        "",
                        {},
                        [],
                        "unknown",
                        f"{type(exc).__name__}: {exc}",
                    )
                results[spec.name] = result
                print(f"[{spec.case_id} {spec.name}] {result.status} ({result.duration_seconds:.1f}s)", flush=True)
                if args.fail_fast and not result.passed:
                    services.cancel_event.set()
            submit_available()
    for spec in selected:
        if spec.name not in results:
            results[spec.name] = ScenarioResult(
                spec.case_id,
                spec.name,
                spec.surface.value,
                "not-started",
                "",
                "",
                0.0,
                "",
                {},
                ["not scheduled because the suite was canceled"],
                "not-needed",
            )
    return [results[spec.name] for spec in selected]


def suite_exit_code(results: Sequence[ScenarioResult], *, credential_unchanged: bool, interrupted: bool) -> int:
    if interrupted:
        return 130
    if not credential_unchanged or any(not result.passed for result in results):
        return 1
    return 0


def _suite_summary(
    args: argparse.Namespace,
    selected: Sequence[ScenarioSpec],
    results: Sequence[ScenarioResult],
    credential_unchanged: bool,
    started: float,
    interrupted: bool,
) -> dict[str, Any]:
    return {
        "pipelineName": PIPELINE_NAME,
        "selectedSuites": args.suite or ([] if args.scenario else ["smoke"]),
        "selectedScenarios": [item.name for item in selected],
        "concurrency": args.concurrency,
        "durationSeconds": round(time.monotonic() - started, 3),
        "interrupted": interrupted,
        "credentialSourceUnchanged": credential_unchanged,
        "counts": {
            status_value: sum(result.status == status_value for result in results)
            for status_value in ("passed", "failed", "canceled", "not-started")
        },
        "results": [dataclasses.asdict(result) for result in results],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_scenarios:
        for spec in SCENARIOS:
            suites = ",".join(sorted(spec.suites))
            print(f"{spec.case_id}\t{spec.name}\t{spec.surface.value}\t{suites}\t{spec.description}")
        return 0
    try:
        selected = select_scenarios(args.scenario, args.suite)
        validate_args(args, selected)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    started = time.monotonic()
    suite_root = (
        Path(args.run_dir).expanduser().resolve()
        if args.run_dir
        else Path(args.run_root).expanduser().resolve()
        / f"suite-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    if not args.run_dir:
        # Case directories are nested under the unique suite root.
        args.run_root = str(suite_root)
    suite_root.mkdir(parents=True, exist_ok=True)
    source_dir = Path(args.credential_source_dir).expanduser().resolve()
    before = snapshot_credentials(source_dir)
    runtime_defaults = read_runtime_defaults(source_dir)
    if args.skip_preflight:
        preflight = {"ok": True, "skipped": True}
        write_json(suite_root / ".preflight" / "suite-preflight.json", preflight)
    else:
        preflight = run_preflight(args, suite_root, source_dir, runtime_defaults, selected)
    if preflight.get("ok") is not True:
        after = snapshot_credentials(source_dir)
        unchanged = credential_snapshot_unchanged(before, after)
        write_json(
            suite_root / "credential-source-audit.json",
            {"credentialFilesPresent": all(item.exists for item in before.values()), "sourceUnchanged": unchanged},
        )
        write_json(
            suite_root / "suite-summary.json",
            {
                "pipelineName": PIPELINE_NAME,
                "selectedScenarios": [item.name for item in selected],
                "preflight": preflight,
                "credentialSourceUnchanged": unchanged,
                "results": [],
            },
        )
        return 1
    preflight_occupied = preflight.get("occupiedVSwitchCidrs", [])
    inherited_occupied = (
        [str(item) for item in preflight_occupied if isinstance(item, str)]
        if isinstance(preflight_occupied, list)
        else []
    )
    occupied_cidrs = [*args.occupied_cidr, *inherited_occupied]
    services = RunnerServices(cidrs=CidrAllocator(occupied_cidrs, args.cleanup_vpc_cidr or "10.250.0.0/16"))
    interrupted = False
    previous_handlers: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        services.cancel_event.set()
        services.terminate_active_processes()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    try:
        results = execute_selected(selected, args, services, runtime_defaults, suite_root)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    after = snapshot_credentials(source_dir)
    unchanged = credential_snapshot_unchanged(before, after)
    write_json(
        suite_root / "credential-source-audit.json",
        {
            "credentialFilesPresent": all(item.exists for item in before.values()),
            "sourceUnchanged": unchanged,
        },
    )
    summary = _suite_summary(args, selected, results, unchanged, started, interrupted)
    write_json(suite_root / "suite-summary.json", summary)
    return suite_exit_code(results, credential_unchanged=unchanged, interrupted=interrupted)


if __name__ == "__main__":
    raise SystemExit(main())
