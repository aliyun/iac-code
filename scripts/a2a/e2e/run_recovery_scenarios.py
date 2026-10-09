#!/usr/bin/env python3
"""Run A2A pipeline recovery and redaction E2E scenarios.

The scenarios in this file intentionally drive the public A2A JSON-RPC HTTP
endpoint. They do not call pipeline internals directly. Recovery scenarios kill
and restart a local A2A server at scenario-specific points. The redaction
regression scenario stops at step 4 before deployment and compares the
canonical snapshot with the public A2A projection without persisting secrets.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from PIL import Image, ImageDraw, ImageFont

E2E_SCRIPTS_DIR = Path(__file__).resolve().parent
A2A_SCRIPTS_DIR = E2E_SCRIPTS_DIR.parent
for scripts_dir in (E2E_SCRIPTS_DIR, A2A_SCRIPTS_DIR, E2E_SCRIPTS_DIR.parents[2]):
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

from common import (  # noqa: E402
    DEFAULT_INITIAL_PROMPT,
    DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT,
    DEFAULT_RECOVERY_PROMPT,
    DEFAULT_SELECTION_PROMPT,
    RUN_LOG_ROOT_NAME,
    ManagedServer,
    StreamSummary,
    _add_completed_snapshot_checks,
    _append_jsonl,
    _compact_text,
    _free_port,
    _new_run_dir,
    _normal_turn_finished,
    _redact_json_value,
    _redact_sensitive_text,
    _server_env,
    _split_python_command,
    _status_message_texts,
    _utc_now,
    _write_json,
    _write_server_config,
    fetch_pipeline_state,
    run_llm_preflight,
    stream_message,
    wait_for_server,
)
from debugger import (  # noqa: E402
    A2A_VERSION_HEADERS,
    _a2a_task_identity,
    _extract_pipeline_envelopes,
    _parse_sse_data_line,
    build_message_stream_payload,
    build_task_cancel_payload,
    build_task_get_payload,
)

from iac_code.a2a.pipeline_journal import A2APipelineJournal  # noqa: E402
from iac_code.a2a.pipeline_paths import existing_a2a_pipeline_dir_for_session  # noqa: E402
from iac_code.services.session_storage import SessionStorage  # noqa: E402
from iac_code.utils.project_paths import get_projects_dir  # noqa: E402
from iac_code.utils.public_paths import redact_known_public_paths  # noqa: E402
from scripts.e2e_question_driver import answer_question, case_facts, network_facts, question_conversation  # noqa: E402

ASK_TRIGGER_PROMPT = "我有个产品要上线"
ASK_FIRST_ANSWER = "我要创建云网络资源；本次只选择已有 VPC 创建一个 VSwitch，不部署 ECS、EIP、SLB 或 Nginx。"
ASK_SECOND_ANSWER = "选择一个已有 VPC，创建一个 VSwitch；地域、可用区和网段你按低成本默认值推荐。"
INTERVENING_ASK_ANSWER = "使用默认配置（可用区和网段自动规划），继续。"
REDACTION_STEP4_PROMPT = (
    "帮我在阿里云上搭个小程序后端环境，要数据库，平时访问不多，每月最好别超过 200 块。2个方案。"
    "两个方案的 ROS 模板都要创建数据库主账号，并为主账号定义 NoEcho 密码参数；"
    "密码由你生成满足约束的随机值，带入预览并完整保留到方案选择，不要让我提供。"
)
REDACTION_STEP4_ASK_ANSWER = "使用默认地域和低成本配置；数据库密码由你生成合规随机值，继续准备 2 个方案，不要部署。"
ROLLBACK_PROMPT = "我改需求了：使用已有 VPC 创建一个安全组，不创建 VSwitch。请基于这个新需求重新规划。"
IMAGE_ROLLBACK_TARGET_CAPTION = (
    "Restart requirement parsing from the intent_parsing step.\n"
    "Then replan for the new requirement above."
)
CONTINUE_PROMPT = "继续"
CLEANUP_RECOVERY_PROMPT = (
    "请只回复“OK，继续”。不要调用任何工具，不要查询任何云资源，不要删除任何资源。"
    "如果系统有后台 cleanup 恢复流程，请让它自行完成。"
)
CLEANUP_PROMPT_METADATA_TYPE = "pipeline_cleanup_prompt"
CLEANUP_EVENT_TYPES = frozenset(
    {
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
        "cleanup_failed",
    }
)
CLEANUP_ACTIVE_STATUSES = frozenset({"pending", "started", "in_progress", "failed"})
FAULT_AFTER_SNAPSHOT_POINT = "after_a2a_pipeline_snapshot_saved"
SELECTION_DURING_BACKUP_SCENARIO = "selection-during-backup"
BACKUP_DELAY_SECONDS = 10.0
BACKUP_DELAY_FIXTURE_ROOT = E2E_SCRIPTS_DIR / "fixtures" / "backup-delay-sitecustomize"
PERFORMANCE_BACKUP_SCENARIOS = frozenset({"scenario1-performance-backup", SELECTION_DURING_BACKUP_SCENARIO})
DEFAULT_TEXT_MODEL = "deepseek-v4-flash-0731"
DEFAULT_MULTIMODAL_MODEL = "qwen3.8-max"
MULTIMODAL_SCENARIOS = frozenset(
    {
        "image-ask-waiting",
        "image-initial",
        "image-interrupt",
        "image-normal-handoff",
        "image-selection-waiting",
    }
)
IMAGE_TEXT_PROMPT = "请读取图片中的文字，并将图片中的文字作为本轮用户输入执行。"
IMAGE_INTERRUPT_PROMPT = (
    "请先读取图片里的新要求。本轮图片是目标变更，不是确认部署；"
    "先按图片中的目标重新规划，不得沿用旧目标直接部署。"
)
STATIC_TEXT_IMAGE_FIXTURE_ROOT = E2E_SCRIPTS_DIR / "fixtures" / "text-images"
STATIC_TEXT_IMAGE_FIXTURES = {
    "initial": DEFAULT_INITIAL_PROMPT,
    "selection": DEFAULT_SELECTION_PROMPT,
    "normal-followup": DEFAULT_NORMAL_FOLLOWUP_PROMPT,
    "ask-first-answer": ASK_FIRST_ANSWER,
    "ask-second-answer": ASK_SECOND_ANSWER,
    "rollback-interrupt": ROLLBACK_PROMPT,
}

VSWITCH_MARKERS = ("ALIYUN::ECS::VSwitch", "VSwitchId", "vsw-", "VSwitch", "交换机")
FINAL_TARGET_EVIDENCE_KEYS = frozenset(
    {
        "action",
        "candidate",
        "candidates",
        "conclusions",
        "cost",
        "core_requirements",
        "deployment_parameters",
        "file_path",
        "missing_deployment_parameters",
        "name",
        "output_path",
        "outputs",
        "parameters",
        "preview_validation",
        "product",
        "products",
        "region",
        "region_id",
        "regionId",
        "resource_id",
        "resource_intents",
        "resource_type",
        "resourceId",
        "resourceType",
        "resource_types",
        "resources",
        "resources_created",
        "role",
        "selected_candidate",
        "stack_id",
        "stackId",
        "status",
        "template",
        "template_path",
        "template_url",
        "type",
    }
)
FINAL_TARGET_EXCLUDED_ACTIONS = frozenset(
    {
        "avoid",
        "exclude",
        "forbid",
        "not_create",
        "skip",
    }
)
FINAL_SELECTED_PLAN_REALIZED_KEYS = frozenset(
    {
        "deployment_parameters",
        "effective_deployment_parameters",
        "missing_deployment_parameters",
        "parameters",
        "preview_validation",
        "resource_types",
        "selected_candidate_result",
    }
)
FINAL_CANDIDATE_RESULT_EVIDENCE_KEYS = frozenset(
    {
        "cost",
        "deployment_parameters",
        "effective_deployment_parameters",
        "failed",
        "missing_deployment_parameters",
        "outputs",
        "parameters",
        "preview_validation",
        "resource_types",
        "resources",
        "resources_created",
        "stackId",
        "stack_id",
        "status",
        "template",
    }
)
STACK_CREATION_SUCCESS_ACTIONS = {"CreateStack", "ContinueCreateStack"}
EVIDENCE_TEXT_FILE_SUFFIXES = {
    ".json",
    ".md",
    ".ros",
    ".tf",
    ".tfvars",
    ".txt",
    ".yaml",
    ".yml",
}
MAX_EVIDENCE_FILE_BYTES = 256 * 1024
SECURITY_GROUP_MARKERS = ("ALIYUN::ECS::SecurityGroup", "SecurityGroupId", "sg-", "安全组")
TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_INPUT_REQUIRED"}
ROS_STACK_DELETED_STATUSES = {"DELETE_COMPLETE"}
REDACTION_PLACEHOLDERS = frozenset({"***", "[REDACTED]", "<redacted>"})
REDACTION_STEP4_SCENARIO = "redaction-step4"
IAC_CODE_WEB_2C4G_STEP4_SCENARIO = "iac-code-web-2c4g-step4"
IAC_CODE_WEB_2C4G_PROMPT = "我要部署一台2核4G的ECS同时部署 iac-code Agent"


@dataclass
class ScenarioRunResult:
    scenario: str
    run_dir: str
    server_url: str
    context_id: str
    pipeline_task_id: str
    passed: bool
    checks: dict[str, bool]
    abort_reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class EventMatch:
    description: str
    event: Any
    summary: StreamSummary


class TextImageFixtureStore:
    def __init__(self, root: Path, static_root: Path = STATIC_TEXT_IMAGE_FIXTURE_ROOT) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.static_root = static_root

    def part(self, key: str, text: str, *, caption: str = "") -> dict[str, Any]:
        safe_key = _safe_fixture_key(key)
        path = self._static_fixture_path(safe_key, text)
        source = "static"
        if path is None:
            path = self.root / f"{safe_key}.png"
            source = "generated"
            if not path.exists():
                path.write_bytes(_render_text_png(text))
        raw = path.read_bytes()
        if caption:
            # Preserve the pre-rendered Chinese instruction on hosts without CJK
            # fonts. The dynamic ownership caption is ASCII and stays in the
            # image, so an image-only new intent receives the same constraint.
            raw = _append_text_image_caption(raw, caption)
            path = self.root / f"{safe_key}-captioned.png"
            path.write_bytes(raw)
            source += "-captioned"
        self._record_manifest(safe_key, text=text, path=path, byte_size=len(raw), source=source)
        return {
            "filename": path.name,
            "mediaType": "image/png",
            "bytes": base64.b64encode(raw).decode("ascii"),
        }

    def _static_fixture_path(self, key: str, text: str) -> Path | None:
        try:
            manifest = json.loads((self.static_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(manifest, dict):
            return None
        entry = manifest.get(key)
        if not isinstance(entry, dict) or entry.get("text") != text or entry.get("mediaType") != "image/png":
            return None
        filename = entry.get("filename")
        if not isinstance(filename, str) or not filename:
            return None
        path = self.static_root / filename
        return path if path.is_file() else None

    def _record_manifest(self, key: str, *, text: str, path: Path, byte_size: int, source: str) -> None:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        manifest[key] = {
            "text": text,
            "path": str(path),
            "mediaType": "image/png",
            "byteSize": byte_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source": source,
        }
        _write_json(self.manifest_path, manifest)


def _safe_fixture_key(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip().lower())
    return safe.strip("-") or "input"


def _render_text_png(text: str) -> bytes:
    font = _load_text_image_font(size=34)
    if any("\u4e00" <= c <= "\u9fff" for c in text) and not _font_supports_chinese(font, text):
        raise ValueError("text image font is missing Chinese glyphs; set IAC_CODE_E2E_FONT_PATH")
    lines = _wrap_text_for_image(text)
    padding = 40
    line_spacing = 12
    probe = Image.new("RGB", (1, 1), "white")
    draw = ImageDraw.Draw(probe)
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    text_width = int(max((right - left for left, _top, right, _bottom in boxes), default=360))
    line_heights = [int(bottom - top) for _left, top, _right, bottom in boxes] or [40]
    width = int(max(760, min(1600, text_width + padding * 2)))
    height = int(max(220, sum(line_heights) + line_spacing * max(0, len(lines) - 1) + padding * 2))
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = padding
    for line, line_height in zip(lines, line_heights, strict=False):
        draw.text((padding, y), line, fill=(16, 24, 39), font=font)
        y += line_height + line_spacing
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _append_text_image_caption(raw: bytes, caption: str) -> bytes:
    if not caption.isascii():
        raise ValueError("dynamic image ownership captions must be ASCII")
    font = _load_text_image_font(size=26)
    lines = caption.splitlines()
    padding = 40
    spacing = 12
    with Image.open(io.BytesIO(raw)) as original:
        original = original.convert("RGB")
        probe = ImageDraw.Draw(original)
        boxes = [probe.textbbox((0, 0), line, font=font) for line in lines]
        width = max(original.width, max(right - left for left, _, right, _ in boxes) + 2 * padding)
        heights = [max(26, bottom - top) for _, top, _, bottom in boxes]
        height = original.height + 2 * padding + sum(heights) + spacing * (len(lines) - 1)
        image = Image.new("RGB", (width, height), "white")
        image.paste(original, (0, 0))
        draw = ImageDraw.Draw(image)
        y = original.height + padding
        for line, box, line_height in zip(lines, boxes, heights, strict=False):
            left, top, _, _ = box
            draw.text((padding - left, y - top), line, font=font, fill=(16, 24, 39))
            y += line_height + spacing
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _wrap_text_for_image(text: str, *, max_chars: int = 26) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        while len(line) > max_chars:
            lines.append(line[:max_chars])
            line = line[max_chars:]
        if line:
            lines.append(line)
    return lines or [""]


def _font_supports_chinese(font: Any, text: str = "中") -> bool:
    try:
        missing = font.getmask(chr(0x10FFFF))
        return all((mask.size != missing.size or bytes(mask) != bytes(missing))
                   for c in set(text) if "\u4e00" <= c <= "\u9fff"
                   for mask in (font.getmask(c),))
    except (ValueError, UnicodeError):
        return False


def _load_text_image_font(*, size: int) -> Any:
    candidates = [
        os.environ.get("IAC_CODE_E2E_FONT_PATH", ""),
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


class BackgroundStream:
    def __init__(
        self,
        *,
        server_url: str,
        cwd: str,
        prompt: str,
        name: str,
        run_dir: Path,
        timeout: float,
        context_id: str = "",
        task_id: str = "",
        images: list[dict[str, Any]] | None = None,
        redaction_env: dict[str, str] | None = None,
    ) -> None:
        self.server_url = server_url
        self.cwd = cwd
        self.prompt = prompt
        self.name = name
        self.run_dir = run_dir
        self.timeout = timeout
        self.context_id = context_id
        self.task_id = task_id
        self.images = images
        self.redaction_env = redaction_env
        self.summary = StreamSummary(name=name, prompt=prompt, request_task_id=task_id)
        self.events: list[Any] = []
        self.exception: BaseException | None = None
        self.request_started_at: float | None = None
        self.request_started_monotonic: float | None = None
        self._condition = threading.Condition()
        self._done = False
        self._thread = threading.Thread(target=self._run, name=f"a2a-e2e-{name}", daemon=True)

    @property
    def done(self) -> bool:
        with self._condition:
            return self._done

    def start(self) -> None:
        self._thread.start()

    def wait_until_request_started(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self.request_started_monotonic is None:
                if self._done:
                    if self.exception is not None:
                        message = f"{self.name} ended before request dispatch: {self.exception}"
                        raise RuntimeError(message) from self.exception
                    raise RuntimeError(f"{self.name} ended before request dispatch")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for request dispatch in {self.name}")
                self._condition.wait(min(remaining, 0.1))

    def join(self, timeout: float | None = None) -> StreamSummary:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError(f"{self.name} did not finish within {timeout}s")
        if self.exception is not None:
            raise RuntimeError(f"{self.name} failed: {self.exception}") from self.exception
        return self.summary

    def wait_for(
        self,
        predicate: Callable[[Any, StreamSummary], bool],
        *,
        description: str,
        timeout: float,
    ) -> EventMatch:
        deadline = time.monotonic() + timeout
        seen = 0
        with self._condition:
            while True:
                while seen < len(self.events):
                    event = self.events[seen]
                    seen += 1
                    if predicate(event, self.summary):
                        return EventMatch(description=description, event=event, summary=self.summary)
                if self._done:
                    if self.exception is not None:
                        message = f"{self.name} ended before {description}: {self.exception}"
                        raise RuntimeError(message) from self.exception
                    raise RuntimeError(f"{self.name} ended before {description}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for {description} in {self.name}")
                self._condition.wait(min(remaining, 1.0))

    def _run(self) -> None:
        payload = build_message_stream_payload(
            cwd=self.cwd,
            prompt=self.prompt,
            context_id=self.context_id,
            task_id=self.task_id,
            request_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
            images=self.images,
        )
        with self._condition:
            self.request_started_at = time.time()
            self.request_started_monotonic = time.monotonic()
            self._condition.notify_all()
        _append_jsonl(
            self.run_dir / "requests.jsonl",
            {"name": self.name, "payload": payload, "at": _utc_now()},
            self.redaction_env,
        )
        request = Request(
            self.server_url.rstrip("/") + "/",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                for line in response:
                    parsed = _parse_sse_data_line(line)
                    if parsed is None:
                        continue
                    _append_jsonl(self.run_dir / f"{self.name}.events.jsonl", parsed, self.redaction_env)
                    with self._condition:
                        self.events.append(parsed)
                        _apply_event(self.summary, parsed)
                        self._condition.notify_all()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            redacted_body = _redact_sensitive_text(body, self.redaction_env)
            self.exception = RuntimeError(f"HTTP {exc.code}: {redacted_body[:500]}")
            _append_jsonl(
                self.run_dir / f"{self.name}.events.jsonl",
                {"error": f"HTTP {exc.code}", "body": redacted_body},
                self.redaction_env,
            )
        except (TimeoutError, URLError, OSError) as exc:
            self.exception = exc
            _append_jsonl(self.run_dir / f"{self.name}.events.jsonl", {"error": str(exc)}, self.redaction_env)
        finally:
            with self._condition:
                self._done = True
                self._condition.notify_all()


class ScenarioHarness:
    def __init__(self, args: argparse.Namespace, *, scenario: str) -> None:
        self.args = args
        self.scenario = scenario
        self.server_cwd = str(Path(args.server_cwd).expanduser().resolve())
        self.run_dir = _scenario_run_dir(args, scenario)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex[:12]
        self.owned_stack_names = (
            [_cleanup_stack_name(self, label) for label in ("first", "second")]
            if scenario in {"rollback-step5-cleanup", "rollback-step5-cleanup-recovery"}
            else [_cleanup_stack_name(self, "main")]
        )
        self.notes: list[str] = []
        self.backup_root: Path | None = None
        self.image_fixtures = TextImageFixtureStore(self.run_dir / "image-fixtures")
        self.workspace_dir = Path(args.cwd).expanduser().resolve() if args.cwd else self.run_dir / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = str(self.workspace_dir)
        self.port = args.port if args.port else _free_port(args.host)
        self.server_url = f"http://{args.host}:{self.port}"
        self.config_path = _write_server_config(
            self.run_dir,
            host=args.host,
            port=self.port,
            auto_approve_permissions=not args.no_auto_approve_permissions,
        )
        self.server_env = _server_env(
            os.environ.copy(),
            provider=args.provider,
            model=_model_for_scenario(args, scenario),
            api_base=args.api_base,
        )
        if getattr(args, "ci_teardown", False):
            from iac_code.config import get_config_dir

            _write_json(self.run_dir / "owned-stacks.json", {
                "configDir": self.server_env.get("IAC_CODE_CONFIG_DIR") or str(get_config_dir()), "cwd": self.cwd,
            })
        if scenario == REDACTION_STEP4_SCENARIO:
            self.server_env["IAC_CODE_A2A_SAFE_MODE"] = "true"
            self.notes.append("forced IAC_CODE_A2A_SAFE_MODE=true for the step4 redaction regression")
        if scenario in PERFORMANCE_BACKUP_SCENARIOS:
            self.backup_root = (self.run_dir / "session-backup").resolve()
            self.backup_root.mkdir(parents=True, exist_ok=True)
            self.server_env["IAC_CODE_A2A_EXTREME_PERFORMANCE"] = "true"
            self.server_env["IAC_CODE_CONFIG_BACKUP_DIR"] = str(self.backup_root)
            self.notes.append(f"enabled A2A extreme performance and backup dir {self.backup_root}")
        if scenario == SELECTION_DURING_BACKUP_SCENARIO:
            fixture_path = str(BACKUP_DELAY_FIXTURE_ROOT.resolve())
            existing_pythonpath = self.server_env.get("PYTHONPATH", "")
            self.server_env["PYTHONPATH"] = os.pathsep.join(
                value for value in (fixture_path, existing_pythonpath) if value
            )
            self.server_env["IAC_CODE_E2E_BACKUP_DELAY_SECONDS"] = str(BACKUP_DELAY_SECONDS)
            control = (self.run_dir / "selection-backup-delay").resolve()
            self.server_env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] = str(control)
            for marker in ("arm", "started", "finished"):
                _backup_delay_marker_path(control, marker).unlink(missing_ok=True)
            _write_json(
                _backup_delay_marker_path(control, "arm"),
                {
                    "armedAt": time.time(),
                    "scenario": scenario,
                    "delaySeconds": BACKUP_DELAY_SECONDS,
                },
            )
            self.notes.append(f"armed E2E-only input_required backup delay fixture for {BACKUP_DELAY_SECONDS:.0f}s")
        if args.deterministic:
            self.server_env["IAC_CODE_A2A_DETERMINISTIC_RECOVERY"] = "1"
            self.server_env["IAC_CODE_TEST_FAULT_INJECTION"] = "1"
            self.server_env["IAC_CODE_TEST_FAULT_INJECTION_MODE"] = "exit"
            fault_at = args.fault_at or (FAULT_AFTER_SNAPSHOT_POINT if scenario == "fault-after-snapshot" else "")
            if fault_at:
                self.server_env["IAC_CODE_TEST_CRASH_AT"] = fault_at
        self.server: ManagedServer | None = None
        self.server_index = 0
        self.context_id = ""
        self.pipeline_task_id = ""
        self.checks: dict[str, bool] = {}
        self.cleanup_status = "not-needed"
        self.cleanup_diagnostic: dict[str, Any] = {}
        self.diagnostics: dict[str, Any] = {}
        self.question_counts: dict[str, int] = {}
        self.current_goal = getattr(args, "initial_prompt", DEFAULT_INITIAL_PROMPT)
        self.summaries: dict[str, Any] = {}
        self.snapshots: dict[str, Any] = {}
        self.failure_stage = ""

    def preflight(self) -> None:
        if getattr(self.args, "ci_teardown", False) and getattr(self.args, "allow_real_cloud", False):
            fixture = network_facts(self.args.python, self.server_env, Path(self.server_cwd), "10.250.1.0/24")
            self.network_fixture_facts = fixture
            config_dir = Path(self.server_env["IAC_CODE_CONFIG_DIR"])
            instruction_name = "IAC-CODE-E2E.md"
            (config_dir / instruction_name).write_text(
                "# E2E fixture isolation\n"
                "如需复用已有 VPC，只能使用独立测试夹具 VpcId=`" + fixture["vpc_id"]
                + "`、ZoneId=`" + fixture["zone_id"] + "`。不得复用其它 E2E Stack 创建的临时 VPC。\n"
                + "如用户未指定 VSwitch 网段，使用本次并发隔离夹具网段 `" + fixture["cidr"] + "`；"
                "不能改用通用默认网段。\n"
                + "不得删除本次测试之外的资源。\n",
                encoding="utf-8",
            )
            self.server_env["IAC_CODE_INSTRUCTION_MEMORY_FILE"] = instruction_name
        if self.args.skip_preflight:
            self.notes.append("LLM preflight skipped")
            return
        preflight = run_llm_preflight(
            python_cmd=_split_python_command(self.args.python),
            cwd=self.server_cwd,
            env=self.server_env,
            timeout=self.args.preflight_timeout,
            run_dir=self.run_dir,
        )
        self.checks["LLM preflight succeeded"] = preflight["ok"] is True
        if not self.checks["LLM preflight succeeded"]:
            raise RuntimeError(f"LLM preflight failed: {preflight['summary']}")

    def start_server(self) -> None:
        self.server_index += 1
        self.server = ManagedServer(
            python_cmd=_split_python_command(self.args.python),
            config_path=self.config_path,
            process_cwd=self.server_cwd,
            allowed_cwd=self.cwd,
            env=self.server_env,
            log_prefix=self.run_dir / f"server-{self.server_index}",
        )
        self.server.start()
        wait_for_server(self.server_url, timeout=self.args.server_timeout, owned_server=self.server)

    def kill9_and_restart(self) -> None:
        self.kill9()
        self.start_server()

    def kill9(self) -> None:
        if self.server is None:
            raise RuntimeError("server is not running")
        self.server.kill9()

    def terminate(self) -> None:
        if self.server is not None and not self.args.leave_server_running:
            self.server.terminate()

    def stream(
        self,
        *,
        prompt: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> StreamSummary:
        if prompt in {ASK_FIRST_ANSWER, ASK_SECOND_ANSWER}:
            self.current_goal = self._ci_owned_prompt(
                ASK_FIRST_ANSWER + ('\n' + ASK_SECOND_ANSWER if prompt == ASK_SECOND_ANSWER else ''))
        prompt = self._ci_owned_prompt(prompt)
        if context_id == "" or "我改需求" in prompt or "停止旧目标" in prompt:
            self.current_goal = prompt
        observation = {}
        if hasattr(self, "_redaction_public_token_counters"):
            observation["on_event"] = lambda event: _merge_usage_token_counters(
                self._redaction_public_token_counters, _usage_event_token_counters(event)
            )
        summary = stream_message(
            server_url=self.server_url,
            cwd=self.cwd,
            prompt=prompt,
            context_id=self.context_id if context_id is None else context_id,
            task_id=self.pipeline_task_id if task_id is None else task_id,
            name=name,
            run_dir=self.run_dir,
            timeout=self.args.stream_timeout,
            images=images,
            redaction_env=self.server_env,
            **observation,
        )
        self._remember_identity(summary)
        self.summaries[name] = summary
        return summary

    def stream_image_text(
        self,
        *,
        text: str,
        image_key: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
        prompt: str = IMAGE_TEXT_PROMPT,
        caption: str = "",
    ) -> StreamSummary:
        return self.stream(
            prompt=prompt,
            name=name,
            context_id=context_id,
            task_id=task_id,
            images=[self._owned_image_part(image_key, text, caption=caption)],
        )

    def start_stream(
        self,
        *,
        prompt: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
        images: list[dict[str, Any]] | None = None,
        wait_for_identity: bool = True,
    ) -> BackgroundStream:
        if prompt in {ASK_FIRST_ANSWER, ASK_SECOND_ANSWER}:
            self.current_goal = self._ci_owned_prompt(
                ASK_FIRST_ANSWER + ('\n' + ASK_SECOND_ANSWER if prompt == ASK_SECOND_ANSWER else ''))
        prompt = self._ci_owned_prompt(prompt)
        if context_id == "" or "我改需求" in prompt or "停止旧目标" in prompt:
            self.current_goal = prompt
        stream = BackgroundStream(
            server_url=self.server_url,
            cwd=self.cwd,
            prompt=prompt,
            context_id=self.context_id if context_id is None else context_id,
            task_id=self.pipeline_task_id if task_id is None else task_id,
            name=name,
            run_dir=self.run_dir,
            timeout=self.args.stream_timeout,
            images=images,
            redaction_env=self.server_env,
        )
        stream.start()
        stream.wait_until_request_started(timeout=self.args.event_timeout)
        if wait_for_identity:
            stream.wait_for(
                lambda _event, summary: bool(summary.context_id and summary.task_id),
                description="task identity",
                timeout=self.args.event_timeout,
            )
            self._remember_identity(stream.summary)
        self.summaries[name] = stream.summary
        return stream

    def _ci_owned_prompt(self, prompt: str) -> str:
        # Cloud ownership is proven by accepted creation receipts, not model instructions.
        return prompt

    def _owned_image_part(self, key: str, text: str, *, caption: str = "") -> dict[str, Any]:
        return self.image_fixtures.part(key, text, caption=caption)

    def start_stream_image_text(
        self,
        *,
        text: str,
        image_key: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
        prompt: str = IMAGE_TEXT_PROMPT,
        caption: str = "",
    ) -> BackgroundStream:
        if image_key == "rollback-interrupt":
            self.current_goal = self._ci_owned_prompt(text)
        return self.start_stream(
            prompt=prompt,
            name=name,
            context_id=context_id,
            task_id=task_id,
            images=[self._owned_image_part(image_key, text, caption=caption)],
        )

    def fetch_state(self, name: str) -> Any:
        snapshot = fetch_pipeline_state(
            server_url=self.server_url,
            context_id=self.context_id,
            task_id=self.pipeline_task_id,
            run_dir=self.run_dir,
            name=name,
            redaction_env=self.server_env,
        )
        self.snapshots[name] = snapshot
        return snapshot

    def capture_task_snapshots(self, label: str) -> dict[str, Any]:
        if not self.context_id:
            raise RuntimeError("context id is unknown")
        if not self.pipeline_task_id:
            raise RuntimeError("pipeline task id is unknown")
        task_get = fetch_task(
            server_url=self.server_url,
            task_id=self.pipeline_task_id,
            run_dir=self.run_dir,
            name=label,
            redaction_env=self.server_env,
        )
        task_list = fetch_tasks(
            server_url=self.server_url,
            context_id=self.context_id,
            run_dir=self.run_dir,
            name=label,
            redaction_env=self.server_env,
        )
        self.snapshots[f"{label}.task-get"] = task_get
        self.snapshots[f"{label}.task-list"] = task_list
        return {"task_get": task_get, "task_list": task_list}

    def wait_for_server_exit(self, *, expected_returncode: int | None = None, timeout: float) -> int:
        if self.server is None or self.server.process is None:
            raise RuntimeError("server is not running")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            returncode = self.server.process.poll()
            if returncode is not None:
                if expected_returncode is not None and returncode != expected_returncode:
                    raise RuntimeError(
                        f"server exited with unexpected return code {returncode}; expected {expected_returncode}"
                    )
                self.server.terminate()
                return returncode
            time.sleep(0.25)
        self.notes.append("server did not exit after deterministic fault; sending SIGKILL for cleanup")
        self.server.kill9()
        raise RuntimeError(f"server did not exit within {timeout:.0f}s after deterministic fault")

    def disable_fault_injection(self) -> None:
        self.server_env.pop("IAC_CODE_TEST_FAULT_INJECTION", None)
        self.server_env.pop("IAC_CODE_TEST_FAULT_INJECTION_MODE", None)
        self.server_env.pop("IAC_CODE_TEST_CRASH_AT", None)
        self.notes.append("disabled fault injection before restart")

    def cancel_pipeline_task(self, name: str) -> Any:
        if not self.pipeline_task_id:
            raise RuntimeError("pipeline task id is unknown")
        payload = build_task_cancel_payload(task_id=self.pipeline_task_id, request_id=str(uuid.uuid4()))
        _append_jsonl(
            self.run_dir / "requests.jsonl",
            {"name": name, "payload": payload, "at": _utc_now()},
            self.server_env,
        )
        request = Request(
            self.server_url.rstrip("/") + "/",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else None
        except Exception as exc:
            data = {"error": str(exc)}
        redacted = _redact_json_value(data, self.server_env)
        _write_json(self.run_dir / f"{name}.cancel-response.json", redacted)
        return redacted

    def _remember_identity(self, summary: StreamSummary) -> None:
        if summary.context_id:
            if self.context_id and summary.context_id != self.context_id:
                self.checks[f"{summary.name} stayed in same context"] = False
            self.context_id = self.context_id or summary.context_id
        if summary.task_id and not self.pipeline_task_id:
            self.pipeline_task_id = summary.task_id

    def finish(
        self, *, passed: bool | None = None, abort_reason: str = "", error_type: str = "", error_site: str = ""
    ) -> int:
        if passed is None:
            passed = bool(self.checks) and all(self.checks.values())
        result = ScenarioRunResult(
            scenario=self.scenario,
            run_dir=str(self.run_dir),
            server_url=self.server_url,
            context_id=self.context_id,
            pipeline_task_id=self.pipeline_task_id,
            passed=passed,
            checks=self.checks,
            abort_reason=abort_reason,
            notes=self.notes,
        )
        payload = {
            **asdict(result),
            "cleanup_status": self.cleanup_status,
            "cleanup_diagnostic": self.cleanup_diagnostic,
            "diagnostics": self.diagnostics,
            "a2a_states": [state for summary in self.summaries.values() for state in summary.status_states][-12:],
            "terminal_markers": _terminal_markers(self.summaries.values()),
            "control_state": _control_state_diagnostic(self.run_dir, self.context_id, self.pipeline_task_id),
            "streams": {name: asdict(summary) for name, summary in self.summaries.items()},
            "snapshots": self.snapshots,
        }
        if not result.passed:
            payload.update(error_type=error_type, error_site=error_site, failure_stage=self.failure_stage)
        _write_json(self.run_dir / "summary.json", payload)
        _print_result(result)
        return 0 if result.passed else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run A2A session recovery E2E scenarios.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(_SCENARIOS),
        help="Scenario to run. Can be repeated. Defaults to scenario1.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="A2A server port. 0 chooses a free port per scenario.")
    parser.add_argument(
        "--cwd",
        default="",
        help="Workspace cwd sent in A2A metadata. Defaults to <run-dir>/workspace.",
    )
    parser.add_argument("--server-cwd", default=str(Path.cwd()))
    parser.add_argument("--run-root", default=str(Path(tempfile.gettempdir()) / RUN_LOG_ROOT_NAME))
    parser.add_argument("--run-dir", default="", help="Explicit run dir. Only valid when running one scenario.")
    parser.add_argument("--python", default="uv run python")
    parser.add_argument("--provider", default="")
    parser.add_argument(
        "--model",
        default="",
        help=(
            "Override the model for every selected scenario. By default, image scenarios use "
            f"{DEFAULT_MULTIMODAL_MODEL} and all other scenarios use {DEFAULT_TEXT_MODEL}."
        ),
    )
    parser.add_argument("--api-base", default="")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help=(
            "Enable deterministic test fault injection. This fixes the crash point only; "
            "it does not mock the pipeline, LLM, tools, or cloud APIs after restart."
        ),
    )
    parser.add_argument(
        "--fault-at",
        default="",
        help="Named deterministic fault point, for example after_a2a_pipeline_snapshot_saved.",
    )
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument(
        "--ci-teardown", action="store_true", help="Use run-scoped Stack names and verified final teardown."
    )
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--preflight-timeout", type=float, default=60.0)
    parser.add_argument("--server-timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=1800.0)
    parser.add_argument("--event-timeout", type=float, default=240.0)
    parser.add_argument("--leave-server-running", action="store_true")
    parser.add_argument("--no-auto-approve-permissions", action="store_true")
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument(
        "--redaction-step4-prompt",
        default=REDACTION_STEP4_PROMPT,
        help="Real backend/database prompt used only by the redaction-step4 scenario.",
    )
    parser.add_argument("--selection-prompt", default=DEFAULT_SELECTION_PROMPT)
    parser.add_argument("--normal-followup-prompt", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    parser.add_argument("--recovery-prompt", default=DEFAULT_RECOVERY_PROMPT)
    parser.add_argument("--expected-text", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    return parser.parse_args(argv)


def _model_for_scenario(args: argparse.Namespace, scenario: str) -> str:
    if args.model:
        return args.model
    return DEFAULT_MULTIMODAL_MODEL if scenario in MULTIMODAL_SCENARIOS else DEFAULT_TEXT_MODEL


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scenarios = args.scenario or ["scenario1"]
    if args.run_dir and len(scenarios) != 1:
        raise SystemExit("--run-dir can only be used with a single --scenario")
    for scenario in scenarios:
        _validate_scenario_execution(args, scenario)
    results: list[int] = []
    for scenario in scenarios:
        runner = _SCENARIOS[scenario]
        results.append(runner(args, scenario))
    return 0 if all(code == 0 for code in results) else 1


def _run_with_harness(args: argparse.Namespace, scenario: str, callback: Callable[[ScenarioHarness], None]) -> int:
    harness = ScenarioHarness(args, scenario=scenario)
    passed: bool | None = None
    abort_reason = ""
    error_type = ""
    error_site = ""
    try:
        harness.preflight()
        harness.start_server()
        callback(harness)
    except Exception as exc:
        harness.notes.append(f"exception: {type(exc).__name__}: {exc}")
        passed = False
        abort_reason = str(exc)
        error_type = type(exc).__name__
        if isinstance(exc, HTTPError):
            harness.diagnostics["http_status_code"] = exc.code
        traceback = exc.__traceback__
        while traceback is not None:
            filename = Path(traceback.tb_frame.f_code.co_filename)
            try:
                relative = filename.resolve().relative_to(E2E_SCRIPTS_DIR.parents[2])
            except ValueError:
                pass
            else:
                if relative.parts[0] in {"scripts", "src"} and relative.suffix == ".py":
                    error_site = f"{relative.as_posix()}:{traceback.tb_lineno}"
            traceback = traceback.tb_next
    finally:
        harness.diagnostics["pre_teardown_control_state"] = _control_state_diagnostic(
            harness.run_dir, harness.context_id, harness.pipeline_task_id)
        try:
            harness.terminate()
        except Exception as exc:
            harness.notes.append("server teardown: " + type(exc).__name__)
            harness.checks["server stopped"] = False
        if getattr(args, "ci_teardown", False):
            try:
                from cleanup_owned_stacks import CleanupOperationError, cleanup_owned_stacks

                cleanup = cleanup_owned_stacks(harness.run_dir)
                harness.cleanup_status = cleanup["status"]
                harness.cleanup_diagnostic = {
                    "failure_count": len(cleanup["failures"]),
                    "remaining_count": len(cleanup["remainingStackIds"]),
                }
                harness.checks["test-owned ROS Stacks cleaned"] = cleanup["status"] == "completed"
            except Exception as exc:
                harness.cleanup_status = "failed"
                harness.cleanup_diagnostic = {
                    "error_type": exc.cause_type if isinstance(exc, CleanupOperationError) else type(exc).__name__,
                    "stage": exc.stage if isinstance(exc, CleanupOperationError) else "other",
                    "sdk_code": exc.sdk_code if isinstance(exc, CleanupOperationError) else "",
                }
                harness.checks["test-owned ROS Stacks cleaned"] = False
                harness.notes.append("teardown: " + type(exc).__name__)
    return harness.finish(passed=passed, abort_reason=abort_reason, error_type=error_type, error_site=error_site)


def _terminal_markers(summaries: Iterable[StreamSummary]) -> list[str]:
    """Expose only fixed failure clues; terminal text can contain user data."""

    text = " ".join(summary.terminal_status_text for summary in summaries).casefold()
    markers = (
        "active session", "execution", "permission", "credential", "timeout", "model",
        "context", "task", "selector", "not found", "terminal state", "rate limit",
        "unsupported", "duplicate",
    )
    result = [marker for marker in markers if marker in text]
    for label, phrase in (
        ("persisted_owner_conflict", "current execution is active in another process"),
        ("unfinished_recovery", "current execution must finish recovery before a new task starts"),
    ):
        if phrase in text:
            result.append(label)
    return result


def _control_state_diagnostic(run_dir: Path, context_id: str, task_id: str) -> dict[str, Any]:
    """Read only fixed, non-secret execution-control fields for CI triage."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", context_id):
        return {"present": False}
    path = run_dir / "a2a-persistence" / "execution-control" / f"{context_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"present": False}
    if not isinstance(record, dict):
        return {"present": False}
    blockers = record.get("blockers")
    external_operations = record.get("externalOperations")
    backup = record.get("backup")
    projected_operations = Counter()
    for operation in external_operations if isinstance(external_operations, list) else []:
        if not isinstance(operation, dict):
            continue
        outcome = operation.get("outcome")
        if isinstance(outcome, str) and outcome in {"accepted", "unknown"}:
            projected_operations["outcome:" + outcome] += 1
        action = operation.get("action")
        if isinstance(action, str) and action in {"CreateStack", "DeleteStack", "UpdateStack", "ContinueCreateStack"}:
            projected_operations["action:" + action] += 1
        if operation.get("resourceId") and operation.get("regionId"):
            projected_operations["identity_present"] += 1
    result = {
        "present": True,
        "task_matches": record.get("taskId") == task_id,
        "phase": record.get("phase"),
        "execution_status": record.get("executionStatus"),
        "release_ready": record.get("releaseReady"),
        "input_handoff_ready": record.get("inputHandoffReady"),
        "stream_available": record.get("streamAvailable"),
        "blocker_count": len(blockers) if isinstance(blockers, list) else None,
        "subprocess_tracking": record.get("subprocessToolTrackingVersion") == 1,
        "active_subprocess_tools": record.get("activeSubprocessTools"),
        "external_operation_count": len(external_operations) if isinstance(external_operations, list) else None,
        "revision_settled": (
            isinstance(record.get("revision"), int)
            and not isinstance(record["revision"], bool)
            and record.get("revision") == record.get("persistedRevision")
        ),
        "backup_status": backup.get("status") if isinstance(backup, dict) else None,
    }
    if projected_operations:
        result["external_operation_categories"] = dict(projected_operations)
    allowed_kinds = {"execution", "agent_loop", "background_agent", "permission_cleanup", "tool", "tool_batch", "llm"}
    blocker_categories = {
        item["kind"]: item["count"] for item in blockers if isinstance(item, dict)
        and item.get("kind") in allowed_kinds and type(item.get("count")) is int and 0 < item["count"] <= 10000
    } if isinstance(blockers, list) else {}
    if blocker_categories:
        result["blocker_categories"] = blocker_categories
    return result


def run_scenario1(args: argparse.Namespace, scenario: str) -> int:
    return _run_scenario1(args, scenario)


def run_scenario1_performance_backup(args: argparse.Namespace, scenario: str) -> int:
    return _run_scenario1(
        args,
        scenario,
        omit_selection_task_id=True,
        check_waiting_input_backup=True,
    )


def _run_scenario1(
    args: argparse.Namespace,
    scenario: str,
    *,
    omit_selection_task_id: bool = False,
    check_waiting_input_backup: bool = False,
) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial stream reached input_required"] = _reached_input_required(initial)
        h.checks["initial input_required is step4 confirm_and_select"] = (
            initial.last_input_required_step_id == "confirm_and_select"
        )
        if check_waiting_input_backup:
            h.checks["performance mode explicitly enabled"] = (
                h.server_env.get("IAC_CODE_A2A_EXTREME_PERFORMANCE") == "true"
            )
            h.checks["backup dir explicitly enabled"] = bool(h.server_env.get("IAC_CODE_CONFIG_BACKUP_DIR"))
            h.snapshots["step4_backup"] = _waiting_input_backup_snapshots(h)
            h.checks["step4 backup task is input-required"] = (
                h.snapshots["step4_backup"].get("task", {}).get("state") == "input-required"
            )
            h.checks["step4 backup context has no active task"] = (
                h.snapshots["step4_backup"].get("context", {}).get("active_task_id") is None
            )
            h.kill9()
            backup_restore = _remove_primary_session_for_backup_restore(h)
            h.snapshots["step4_backup_only_restore"] = backup_restore
            h.start_server()
            primary_session_dir = Path(backup_restore["primarySessionDir"])
            primary_session_file = Path(backup_restore["primarySessionFile"])
            backup_restore["primaryAbsentAfterRestart"] = not primary_session_dir.exists()
            backup_restore["primarySessionFileAbsentAfterRestart"] = not primary_session_file.exists()
            h.checks["primary session stayed absent after restart"] = (
                backup_restore["primaryAbsentAfterRestart"]
                and backup_restore["primarySessionFileAbsentAfterRestart"]
            )
            _write_json(h.run_dir / "step4.backup-only-restore.json", backup_restore)
        selection = h.stream(
            prompt=args.selection_prompt,
            name="02-select-candidate",
            task_id="" if omit_selection_task_id else None,
        )
        if omit_selection_task_id:
            _add_hydrated_task_checks(h, selection, "selection")
            h.checks["selection did not report already-working error"] = not _has_already_working_error(selection)
            h.checks["selection did not report terminal-state error"] = not _has_terminal_state_error(selection)
        if check_waiting_input_backup:
            backup_restore["primaryRestoredAfterSelection"] = primary_session_dir.is_dir()
            backup_restore["primarySessionFileRestoredAfterSelection"] = primary_session_file.is_file()
            backup_restore["backupStillPresentAfterSelection"] = Path(backup_restore["backupSessionDir"]).is_dir()
            h.checks["selection restored primary session from backup"] = (
                backup_restore["primaryRestoredAfterSelection"]
                and backup_restore["primarySessionFileRestoredAfterSelection"]
                and backup_restore["backupStillPresentAfterSelection"]
            )
            _write_json(h.run_dir / "step4.backup-only-restore.json", backup_restore)
        selection = _finish_pipeline_after_possible_input(h, selection, args)
        h.checks["selection completed pipeline"] = _pipeline_completed(selection)
        h.checks["selection produced normal handoff"] = selection.normal_handoff_ready
        if not h.checks["selection completed pipeline"] or not h.checks["selection produced normal handoff"]:
            raise RuntimeError("pipeline did not complete with normal handoff before follow-up")
        h.snapshots["after_pipeline"] = h.fetch_state("after-pipeline")
        _add_completed_snapshot_checks(
            h.checks,
            "after-pipeline state",
            h.snapshots["after_pipeline"],
            context_id=h.context_id,
            task_id=h.pipeline_task_id,
        )
        h.checks["after-pipeline state has no cleanup activity"] = not _snapshot_has_cleanup_activity(
            h.snapshots["after_pipeline"]
        )
        normal = h.stream(prompt=args.normal_followup_prompt, name="03-normal-followup", task_id="")
        h.checks["normal follow-up stayed in same context"] = normal.context_id == h.context_id
        h.checks["normal follow-up used a new task"] = bool(normal.task_id) and normal.task_id != h.pipeline_task_id
        h.checks["normal follow-up finished turn"] = _normal_turn_finished(normal)
        h.checks["normal follow-up produced text"] = bool(normal.text.strip())
        h.kill9_and_restart()
        h.snapshots["after_restart"] = h.fetch_state("after-restart")
        _add_completed_snapshot_checks(
            h.checks,
            "after-restart state",
            h.snapshots["after_restart"],
            context_id=h.context_id,
            task_id=h.pipeline_task_id,
        )
        h.checks["after-restart state has no cleanup activity"] = not _snapshot_has_cleanup_activity(
            h.snapshots["after_restart"]
        )
        recovery = h.stream(prompt=args.recovery_prompt, name="04-recovery-question", task_id="")
        h.checks["recovery stayed in same context"] = recovery.context_id == h.context_id
        h.checks["recovery used a new task"] = bool(recovery.task_id) and recovery.task_id not in {
            h.pipeline_task_id,
            normal.task_id,
        }
        h.checks["recovery finished turn"] = _normal_turn_finished(recovery)
        h.checks["normal follow-up persisted in session"] = _a2a_session_contains_user_message(
            h, args.normal_followup_prompt
        )
        h.checks["recovery question persisted in session"] = _a2a_session_contains_user_message(
            h, args.recovery_prompt
        )
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)
        h.checks["scenario1 emitted no cleanup events"] = not _run_dir_has_cleanup_events(h.run_dir)
        h.checks["scenario1 persisted no cleanup prompt"] = not _session_has_cleanup_prompt(h)
        h.checks["scenario1 ledger has no cleanup-required resources"] = not _cleanup_ledger_has_required_resources(h)

    return _run_with_harness(args, scenario, callback)


def run_running_step(args: argparse.Namespace, scenario: str) -> int:
    step_id = _RUNNING_STEP_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        if step_id == "deploying":
            initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
            initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
            h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
            stream = h.start_stream(prompt=args.selection_prompt, name="02-select-candidate-running")
        else:
            stream = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        if step_id == "evaluate_candidates":
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _candidate_started,
                description="candidate started in evaluate_candidates",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        else:
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _step_started(step_id),
                description=f"step_started({step_id})",
                # Step 4 is preceded by intent, architecture and real candidate
                # evaluation. Use the declared stream budget for the whole
                # preparation, retaining the event budget as a no-progress bound.
                timeout=args.stream_timeout if step_id == "confirm_and_select" else args.event_timeout,
                name_prefix="initial-running",
                progress_idle_timeout=args.event_timeout if step_id == "confirm_and_select" else None,
            )
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for observed_stream in observed_streams:
            _join_after_kill(observed_stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after restart"] = _snapshot(snapshot) is not None
        h.checks["pipeline taskId persisted"] = _snapshot_value(snapshot, "taskId") == h.pipeline_task_id
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="03-continue-after-restart")
        _finish_pipeline_after_possible_input(h, resumed, args, input_prompt=ROLLBACK_PROMPT)
        h.checks["pipeline completed after recovery"] = _completed_snapshot_or_stream(h, resumed)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_normal_running(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        _complete_pipeline(h, args)
        normal = h.start_stream(prompt=args.normal_followup_prompt, name="03-normal-followup-running", task_id="")
        normal.wait_for(_normal_text_started, description="normal chat text started", timeout=args.event_timeout)
        h.kill9_and_restart()
        _join_after_kill(normal, h)
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="04-normal-continue", task_id="")
        h.checks["normal continue stayed in same context"] = resumed.context_id == h.context_id
        final = h.stream(prompt=DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT, name="05-normal-history-check", task_id="")
        h.checks["history check stayed in same context"] = final.context_id == h.context_id
        h.checks["normal follow-up persisted in session"] = _a2a_session_contains_user_message(
            h, args.normal_followup_prompt
        )
        h.checks["history check persisted in session"] = _a2a_session_contains_user_message(
            h, DEFAULT_NORMAL_RUNNING_RECOVERY_PROMPT
        )

    return _run_with_harness(args, scenario, callback)


def run_ask_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=ASK_TRIGGER_PROMPT, name="01-ask-trigger", context_id="", task_id="")
        h.checks["initial reached input_required"] = _reached_input_required(initial)
        h.checks["input_required is ask_user_question"] = (
            _latest_pending_kind(h.run_dir / "01-ask-trigger.events.jsonl") == "ask_user_question"
        )
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot still waiting input"] = _snapshot_value(snapshot, "status") == "waiting_input"
        h.checks["pending input is ask_user_question"] = _pending_kind(snapshot) == "ask_user_question"
        answer = h.stream(prompt=ASK_FIRST_ANSWER, name="02-answer-first-ask", task_id="")
        _add_hydrated_task_checks(h, answer, "first ask answer")
        final_summary = answer
        if _waiting_for_followup_ask(h, answer):
            second = h.stream(prompt=ASK_SECOND_ANSWER, name="03-answer-second-ask")
            _add_same_task_checks(h, second, "second ask answer")
            _finish_pipeline_after_possible_input(h, second, args)
            final_summary = second
        else:
            _finish_pipeline_after_possible_input(h, answer, args)
        h.checks["pipeline completed after ask recovery"] = _completed_snapshot_or_stream(h, final_summary)
        h.checks["deployment succeeded with Stack ID"] = _deployment_succeeded_with_stack_id(h.run_dir)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_selection_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial reached step4 input_required"] = initial.last_input_required_step_id == "confirm_and_select"
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot still waiting input"] = _snapshot_value(snapshot, "status") == "waiting_input"
        h.checks["pending input is confirm_and_select"] = _pending_step_id(snapshot) == "confirm_and_select"
        selection = h.stream(prompt=args.selection_prompt, name="02-select-after-restart", task_id="")
        _add_hydrated_task_checks(h, selection, "selection answer")
        h.checks["selection accepted and advanced past waiting step"] = _selection_advanced_past_waiting_step(selection)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_selection_during_backup(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        control = _backup_delay_control_path(h)
        initial_stream = h.start_stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        started, initial_streams = _wait_for_backup_start_with_intervening_asks(
            h, control, initial_stream,
            # Real Step 1 planning may exceed the shared 240s event timeout.
            timeout=max(args.event_timeout, min(args.stream_timeout, 600.0)),
        )
        h.snapshots["backup_delay_started"] = started
        h.checks["input_required backup delay started"] = started.get("delaySeconds") == BACKUP_DELAY_SECONDS
        h.checks["active stream was open when backup delay started"] = not initial_streams[-1].done

        h.checks["backup was unfinished when selection request was dispatched"] = not _backup_delay_marker_path(
            control, "finished"
        ).exists()
        selection_stream = h.start_stream(
            prompt=args.selection_prompt,
            name="02-select-during-backup",
            wait_for_identity=False,
        )

        continued_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [initial_streams[-1]],
            _input_required_step("confirm_and_select"),
            description="step4 candidate selection input_required",
            timeout=args.event_timeout,
            name_prefix="01-initial",
        )
        initial_streams = [*initial_streams[:-1], *continued_streams]
        h.checks["initial reached step4 input_required"] = any(
            stream.summary.last_input_required_step_id == "confirm_and_select" for stream in initial_streams
        )

        for stream in initial_streams:
            stream.join(timeout=args.stream_timeout)
        selection = selection_stream.join(timeout=args.stream_timeout)
        finished = _wait_for_backup_delay_marker(control, "finished", timeout=min(10.0, args.event_timeout))
        h.snapshots["backup_delay_finished"] = finished
        h.snapshots["selection_dispatch"] = {
            "dispatchedAt": selection_stream.request_started_at,
            "dispatchedMonotonic": selection_stream.request_started_monotonic,
        }

        started_monotonic = _float_value(started.get("startedMonotonic"))
        finished_monotonic = _float_value(finished.get("finishedMonotonic"))
        selection_dispatched_monotonic = selection_stream.request_started_monotonic
        delay_elapsed = _float_value(finished.get("elapsedSeconds"))
        h.checks["selection request was dispatched during backup delay"] = (
            started_monotonic is not None
            and finished_monotonic is not None
            and selection_dispatched_monotonic is not None
            and started_monotonic <= selection_dispatched_monotonic < finished_monotonic
        )
        h.checks["backup delay lasted at least 10 seconds"] = (
            delay_elapsed is not None and delay_elapsed >= BACKUP_DELAY_SECONDS
        )
        h.checks["selection stayed on pipeline task"] = selection.task_id == h.pipeline_task_id
        # The queued selection is consumed by the pipeline turn that is already
        # streaming on the initial request, so its lifecycle events and the
        # final completion land on the initial stream instead of the selection
        # response. Evaluate the pipeline task across both streams of the same
        # task, exactly as the interrupt-routing check below already does.
        combined = StreamSummary(name="01+02-combined", prompt=selection.prompt)
        combined.task_id = selection.task_id
        combined.context_id = selection.context_id
        combined.pipeline_event_types = [
            *selection.pipeline_event_types,
            *[event for stream in initial_streams for event in stream.summary.pipeline_event_types],
        ]
        combined.status_states = [
            *selection.status_states,
            *[state for stream in initial_streams for state in stream.summary.status_states],
        ]
        h.checks["selection was consumed as candidate input"] = _selection_advanced_past_waiting_step(combined)
        observed_event_types = set(combined.pipeline_event_types)
        h.checks["selection did not enter interrupt routing"] = not {
            "interrupt_received",
            "interrupt_classified",
        }.intersection(observed_event_types)
        h.checks["selection completed pipeline"] = _pipeline_completed(combined)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_redaction_step4(args: argparse.Namespace, scenario: str) -> int:
    """Reach candidate selection and audit canonical versus safe-mode A2A data."""

    def callback(h: ScenarioHarness) -> None:
        # Capture only counter fields in memory before the credential-safe log
        # serializer replaces TOKEN-named fields. Never export raw SSE payloads.
        h._redaction_public_token_counters = {}
        initial = h.stream(prompt=args.redaction_step4_prompt, name="01-redaction-step4", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(
            h,
            initial,
            name_prefix="01-redaction-step4",
            answer_prompt=REDACTION_STEP4_ASK_ANSWER,
        )
        h.checks["initial reached step4 candidate selection"] = (
            initial.last_input_required_step_id == "confirm_and_select"
        )

        canonical_snapshot = _load_canonical_pipeline_snapshot(h)
        _record_noecho_redaction_diagnostics(h, canonical_snapshot)
        _record_redaction_candidate_diagnostics(h, canonical_snapshot)
        public_state = _fetch_pipeline_state_for_redaction_audit(h)
        public_snapshot = _snapshot(public_state)
        if public_snapshot is None:
            raise RuntimeError("public pipeline state did not contain a snapshot")

        audit = _build_step4_redaction_audit(
            canonical_snapshot,
            public_snapshot,
            known_server_paths=(h.cwd, h.server_cwd, str(h.run_dir.resolve())),
            safe_mode=h.server_env.get("IAC_CODE_A2A_SAFE_MODE", ""),
            canonical_token_events=_load_canonical_usage_token_counters(h),
            public_token_events=h._redaction_public_token_counters,
        )
        h.diagnostics.update({
            "redaction_canonical_usage_counter_count": audit["canonicalTokenCounterCount"],
            "redaction_public_usage_counter_count": audit["publicTokenCounterCount"],
            "redaction_legacy_non_numeric_token_field_count": sum(
                key.casefold().endswith("tokens") and not _is_number(value)
                for _path, key, value in _iter_scalar_values(canonical_snapshot)
            ),
        })
        h.snapshots["redaction_audit"] = audit
        _write_json(h.run_dir / "redaction-audit.json", audit)
        h.checks.update(_step4_redaction_checks(audit))
        h.checks["public snapshot is waiting at step4"] = (
            public_snapshot.get("status") == "waiting_input" and _pending_step_id(public_state) == "confirm_and_select"
        )
        pending_input = public_snapshot.get("pendingInput")
        options = pending_input.get("options") if isinstance(pending_input, dict) else None
        h.diagnostics["candidate_option_count"] = len(options) if isinstance(options, list) else 0
        canonical_pending = canonical_snapshot.get("pendingInput")
        canonical_options = canonical_pending.get("options") if isinstance(canonical_pending, dict) else None
        h.diagnostics["redaction_canonical_option_count"] = (
            len(canonical_options) if isinstance(canonical_options, list) else 0
        )
        h.checks["step4 exposes two candidate options"] = isinstance(options, list) and len(options) == 2
        h.notes.append(
            "stopped at step4 candidate selection; no selection input was sent and deployment was not started"
        )

    return _run_with_harness(args, scenario, callback)


def run_iac_code_web_2c4g_step4(args: argparse.Namespace, scenario: str) -> int:
    """Verify the real iac-code Web 2C4G flow stops at candidate selection."""

    def callback(h: ScenarioHarness) -> None:
        original = h.stream(prompt=IAC_CODE_WEB_2C4G_PROMPT, name="01-initial-2c4g", context_id="", task_id="")
        final = _answer_intervening_ask_inputs(h, original, name_prefix="01-initial-2c4g")
        h.checks["exact 2C4G user prompt was sent"] = original.prompt == IAC_CODE_WEB_2C4G_PROMPT
        h.checks["reached step4 candidate selection"] = final.last_input_required_step_id == "confirm_and_select"

        state = h.fetch_state("step4")
        h.checks["pipeline is waiting at step4"] = (
            _snapshot_value(state, "status") == "waiting_input" and _pending_step_id(state) == "confirm_and_select"
        )

        canonical_snapshot = _load_canonical_pipeline_snapshot(h)
        tool_results = _ordered_tool_results(canonical_snapshot)
        event_types = _all_pipeline_event_types(h.summaries.values())
        h.checks["golden iac-code Web solution is evidenced"] = _golden_solution_evidenced(canonical_snapshot)
        h.diagnostics.update(_golden_solution_diagnostics(canonical_snapshot))
        h.diagnostics.update(_golden_template_structure_diagnostics(canonical_snapshot))
        # Product completion supports LLM OR code constraint verification.
        # An accepted model claim or a truncated tool preview is not an oracle.
        h.checks["structured 2 vCPU and 4 GiB evidence"] = _verify_final_2c4g_with_sdk(h, canonical_snapshot)
        successful = [item for item in tool_results if item.get("toolName") == "complete_step"
                      and item.get("isError") is False and isinstance(item.get("input"), dict)]
        conclusions = [item["input"].get("conclusion") for item in successful]
        h.diagnostics["2c4g_successful_completion_count"] = len(successful)
        h.diagnostics["2c4g_parameters_present"] = any(
            isinstance(c, dict) and isinstance(c.get("deployment_parameters"), dict) for c in conclusions)
        h.diagnostics["2c4g_checks_present"] = any(
            isinstance(c, dict) and isinstance(c.get("hard_constraint_checks"), list) for c in conclusions)
        h.diagnostics["2c4g_input_verified"] = any(_verified_2c4g_instance_type(c) for c in conclusions)
        h.diagnostics["2c4g_query_seen"] = any(
            item.get("toolName") == "aliyun_api" and item.get("isError") is False
            and isinstance(item.get("input"), dict)
            and str(item["input"].get("action") or "").casefold() == "describeinstancetypes"
            for item in tool_results)
        categories = Counter()
        for conclusion in conclusions:
            checks = conclusion.get("hard_constraint_checks") if isinstance(conclusion, dict) else None
            for check in checks if isinstance(checks, list) else []:
                if not isinstance(check, dict):
                    continue
                for category_field, allowed in (
                    ("status", {"satisfied", "unsatisfied", "unverified"}),
                    ("actual_unit", {"count", "gib", "GiB", "GB", "MiB"}),
                ):
                    value = check.get(category_field)
                    label = value if isinstance(value, str) and value in allowed else "other"
                    categories[category_field + ":" + label] += 1
                value = _numeric(check.get("actual_value"))
                categories["actual_value:" + (str(int(value)) if value in {2, 4} else "other")] += 1
                values = check.get("parameter_values")
                parameters = conclusion.get("deployment_parameters")
                categories["parameter_binding:" + ("InstanceType" if isinstance(values, dict)
                    and isinstance(parameters, dict) and values.get("InstanceType") == parameters.get("InstanceType")
                    and values.get("InstanceType") else "other")] += 1
                constraint = check.get("constraint")
                if isinstance(constraint, dict):
                    for category_field, allowed in (
                        ("property", {"vcpu", "cpu", "cpu_core_count", "CpuCoreCount", "memory", "MemorySize"}),
                        ("verification_mode", {"direct", "tool", "llm"}),
                        ("unit", {"count", "gib", "GiB", "GB", "MiB"}),
                    ):
                        value = constraint.get(category_field)
                        label = value if isinstance(value, str) and value in allowed else "other"
                        categories[category_field + ":" + label] += 1
                for record in check.get("evidence", []) if isinstance(check.get("evidence"), list) else []:
                    if isinstance(record, dict):
                        kind = record.get('type')
                        categories['evidence_type:' + (kind if isinstance(kind, str)
                            and kind in {'tool', 'llm', 'direct'} else 'other')] += 1
                        name = record.get("tool_name")
                        categories["evidence:" + (name if isinstance(name, str) and name in {
                            "aliyun_api", "bash", "read_file", "ros_get_template_parameter_constraints",
                            "ros_preview_template", "ros_estimate_template_cost"} else "other")] += 1
                        for field, allowed in (
                            ("product", {"ecs", "ros"}),
                            ("action", {"DescribeInstanceTypes", "GetTemplateParameterConstraints", "PreviewStack"}),
                        ):
                            value = record.get(field)
                            categories["evidence_" + field + ":" + (
                                value if isinstance(value, str) and value in allowed else "other"
                            )] += 1
                        path = record.get("result_path")
                        leaf = path.rsplit(".", 1)[-1] if isinstance(path, str) else ""
                        categories["evidence_result_leaf:" + (
                            leaf if leaf in {"CpuCoreCount", "MemorySize", "AllowedValues"} else "other"
                        )] += 1
        h.diagnostics["2c4g_constraint_categories"] = dict(categories)

        tool_order = [str(item.get("toolName") or "") for item in tool_results]
        preview_index = _first_index(tool_order, "ros_preview_template")
        pricing_index = _first_index(tool_order, "ros_estimate_template_cost")
        h.checks["PreviewStack was attempted before pricing"] = (
            preview_index is not None and pricing_index is not None and preview_index < pricing_index
        )
        h.checks["no deployment was started"] = not event_types.intersection(
            {"deploying_started", "deployment_started", "ros_deploy", "stack_created"}
        ) and not any(item.get("toolName") == "ros_deploy" for item in tool_results)
        h.notes.append("stopped at confirm_and_select; no candidate selection or deployment request was sent")

    return _run_with_harness(args, scenario, callback)


def _all_pipeline_event_types(summaries: Iterable[StreamSummary]) -> set[str]:
    return {event_type for summary in summaries for event_type in summary.pipeline_event_types}


def _ordered_tool_results(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    display = snapshot.get("display")
    raw_results = display.get("toolResults") if isinstance(display, dict) else None
    results = [item for item in raw_results or [] if isinstance(item, dict)]
    return sorted(
        results,
        key=lambda item: float(item.get("sequence"))
        if isinstance(item.get("sequence"), (int, float))
        else float("inf"),
    )


def _golden_solution_evidenced(snapshot: dict[str, Any]) -> bool:
    tool_results = _ordered_tool_results(snapshot)
    golden_path = "references/solutions/iac-code-web.ros.yml"
    read_golden = any(
        item.get("toolName") == "read_file"
        and item.get("isError") is False
        and str((item.get("input") or {}).get("path") or "").replace("\\", "/").endswith(golden_path)
        for item in tool_results
        if isinstance(item.get("input"), dict)
    )
    produced_tagged_template = any(
        item.get("toolName") in {"write_file", "complete_step"}
        and item.get("isError") is False
        and "acs:solution:iac-code:iac-code-web" in json.dumps(item.get("input"), ensure_ascii=False, default=str)
        for item in tool_results
        if isinstance(item.get("input"), dict)
    )
    return read_golden and produced_tagged_template


def _golden_solution_diagnostics(snapshot: dict[str, Any]) -> dict[str, bool]:
    """Retain which native evidence is missing without exporting file contents or paths."""
    tools = _ordered_tool_results(snapshot)
    successful = [item for item in tools if item.get("isError") is False and isinstance(item.get("input"), dict)]
    golden_path = "references/solutions/iac-code-web.ros.yml"
    tag = "acs:solution:iac-code:iac-code-web"
    def has_tag(item: dict[str, Any]) -> bool:
        return tag in json.dumps(item["input"], ensure_ascii=False, default=str)
    return {
        "golden_read_path_seen": any(item.get("toolName") == "read_file" and
            str(item["input"].get("path") or "").replace("\\", "/").endswith(golden_path) for item in successful),
        "golden_read_alias_seen": any(item.get("toolName") == "read_file" and
            str(item["input"].get("file_path") or "").replace("\\", "/").endswith(golden_path)
            for item in successful),
        "golden_tagged_write_seen": any(item.get("toolName") == "write_file" and has_tag(item)
            for item in successful),
        "golden_tagged_completion_seen": any(item.get("toolName") == "complete_step" and has_tag(item)
            for item in successful),
        "golden_bash_path_seen": any(item.get("toolName") == "bash" and golden_path in
            json.dumps(item["input"], ensure_ascii=False, default=str) for item in successful),
    }


def _golden_template_structure_diagnostics(snapshot: dict[str, Any]) -> dict[str, bool]:
    """Compare native generated bodies in memory; export no body, path, or resource ID.

    These facts are diagnostic only. In particular a matching resource list cannot
    substitute for the existing Golden solution acceptance check.
    """
    bodies: list[str] = []
    golden_candidate = False

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal golden_candidate
        if depth > 12:
            return
        if isinstance(value, dict):
            golden_candidate |= value.get("name") == "iac-code-web-single-ecs"
            for key, child in value.items():
                if key == "template" and isinstance(child, str) and len(child) <= 1_000_000:
                    bodies.append(child)
                elif isinstance(child, (dict, list)):
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:100]:
                visit(child, depth + 1)

    for item in _ordered_tool_results(snapshot):
        if item.get("isError") is not False or not isinstance(item.get("input"), dict):
            continue
        inputs = item["input"]
        if item.get("toolName") == "write_file":
            body = inputs.get("content")
            if isinstance(body, str) and len(body) <= 1_000_000:
                bodies.append(body)
        elif item.get("toolName") == "complete_step":
            visit(inputs.get("conclusion"))
            visit(item.get("normalizedConclusion"))

    facts = {
        "golden_candidate_name_seen": golden_candidate,
        "golden_generated_body_seen": bool(bodies),
        "golden_generated_template_parsed": False,
        "golden_generated_metadata_tag_seen": False,
        "golden_generated_resource_types_match": False,
        "golden_generated_bootstrap_matches": False,
        "golden_generated_bootstrap_type_matches": False,
        "golden_generated_bootstrap_dependencies_match": False,
        "golden_generated_bootstrap_properties_match": False,
        "golden_generated_bootstrap_script_matches": False,
        "golden_generated_bootstrap_bindings_match": False,
        "golden_generated_outputs_match": False,
    }
    baseline_path = E2E_SCRIPTS_DIR.parents[2] / (
        "src/iac_code/skills/bundled/iac_aliyun/references/solutions/iac-code-web.ros.yml"
    )
    baseline = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
    # Compare the latest native template body; a stale initial write must not mask
    # later removal of the application resources or its bootstrap.
    try:
        template = yaml.safe_load(bodies[-1]) if bodies else None
    except yaml.YAMLError:
        template = None
    if not isinstance(template, dict) or not isinstance(template.get("Resources"), dict):
        return facts
    facts["golden_generated_template_parsed"] = True
    resources = template["Resources"]
    metadata = template.get("Metadata")
    interface = metadata.get("ALIYUN::ROS::Interface") if isinstance(metadata, dict) else None
    tags = interface.get("TemplateTags") if isinstance(interface, dict) else None
    facts["golden_generated_metadata_tag_seen"] = (
        isinstance(tags, list) and "acs:solution:iac-code:iac-code-web" in tags
    )

    def resource_types(value: dict[str, Any]) -> Counter:
        return Counter(
            resource["Type"] for resource in value.values()
            if isinstance(resource, dict) and isinstance(resource.get("Type"), str)
        )

    facts["golden_generated_resource_types_match"] = (
        resource_types(resources) == resource_types(baseline["Resources"])
    )
    facts["golden_generated_bootstrap_matches"] = resources.get("Bootstrap") == baseline["Resources"]["Bootstrap"]
    actual_bootstrap = resources.get("Bootstrap")
    expected_bootstrap = baseline["Resources"]["Bootstrap"]
    if isinstance(actual_bootstrap, dict):
        facts["golden_generated_bootstrap_type_matches"] = (
            actual_bootstrap.get("Type") == expected_bootstrap["Type"]
        )
        depends_on = actual_bootstrap.get("DependsOn")
        facts["golden_generated_bootstrap_dependencies_match"] = (
            isinstance(depends_on, list) and all(isinstance(item, str) for item in depends_on)
            and sorted(depends_on) == sorted(expected_bootstrap["DependsOn"])
        )
        properties = actual_bootstrap.get("Properties")
        expected_properties = expected_bootstrap["Properties"]
        if isinstance(properties, dict):
            facts["golden_generated_bootstrap_properties_match"] = (
                {key: value for key, value in properties.items() if key != "CommandContent"}
                == {key: value for key, value in expected_properties.items() if key != "CommandContent"}
            )
            content = properties.get("CommandContent")
            sub = content.get("Fn::Sub") if isinstance(content, dict) else None
            expected_sub = expected_properties["CommandContent"]["Fn::Sub"]
            if isinstance(sub, list) and len(sub) == 2:
                facts["golden_generated_bootstrap_script_matches"] = sub[0] == expected_sub[0]
                facts["golden_generated_bootstrap_bindings_match"] = sub[1] == expected_sub[1]
    facts["golden_generated_outputs_match"] = template.get("Outputs") == baseline["Outputs"]
    return facts


def _has_2c4g_structured_evidence(snapshot: dict[str, Any]) -> bool:
    """Require a verified final parameter plus matching DescribeInstanceTypes data."""

    tool_results = _ordered_tool_results(snapshot)
    for item in tool_results:
        if (
            item.get("toolName") != "complete_step"
            or item.get("isError") is not False
            or not isinstance(item.get("input"), dict)
        ):
            continue
        conclusion = item["input"].get("conclusion")
        instance_type = _verified_2c4g_instance_type(conclusion)
        if instance_type and _instance_type_result_is_2c4g(tool_results, instance_type):
            return True
    return False


def _verify_final_2c4g_with_sdk(h: ScenarioHarness, snapshot: dict[str, Any]) -> bool:
    """Independent read-only oracle, not a claim that the agent called a specific API."""
    conclusions = [item["input"].get("conclusion") for item in _ordered_tool_results(snapshot)
                   if item.get("toolName") == "complete_step" and item.get("isError") is False
                   and isinstance(item.get("input"), dict)]
    costs = [c for c in conclusions if isinstance(c, dict)
             and isinstance(c.get("deployment_parameters"), dict) and isinstance(c.get("hard_constraint_checks"), list)]
    h.diagnostics["2c4g_cost_completion_count"] = len(costs)
    claimed_types = [_verified_2c4g_instance_type(c, require_tool_evidence=False) for c in costs]
    model_verified = bool(claimed_types) and all(claimed_types)
    # The accepted deployment parameters select the actual SKU. Its real
    # CPU/memory data decides acceptance; model-shaped claims are diagnostic.
    types = [c["deployment_parameters"].get("InstanceType") for c in costs]
    if not types or any(not isinstance(value, str) or not re.fullmatch(
        r"ecs\.[A-Za-z0-9_.-]{1,100}", value
    ) for value in types):
        h.diagnostics["2c4g_sdk_probe_category"] = "missing_or_invalid_instance_type"
        return False
    types = sorted(set(types))
    if len(types) > 8:
        return False
    code = '''
import contextlib, io, json, sys
with contextlib.redirect_stdout(io.StringIO()):
    from scripts.repl.e2e.run_pipeline_scenarios import _call_aliyun_api
    from scripts.a2a.e2e.run_recovery_scenarios import _mapping_values, _numeric
    body = _call_aliyun_api('ecs', 'DescribeInstanceTypes', {'InstanceTypes': sys.argv[1:]})
    correct = {r.get('InstanceTypeId') for r in _mapping_values(body)
               if _numeric(r.get('CpuCoreCount')) == 2 and _numeric(r.get('MemorySize')) == 4}
    rows = {r.get('InstanceTypeId'): r for r in _mapping_values(body)
            if r.get('InstanceTypeId') in sys.argv[1:]}
    mismatched_cpu = sum(_numeric(r.get('CpuCoreCount')) != 2 for r in rows.values())
    mismatched_memory = sum(_numeric(r.get('MemorySize')) != 4 for r in rows.values())
    sizes = []
    for row in rows.values():
        cpu, memory = _numeric(row.get('CpuCoreCount')), _numeric(row.get('MemorySize'))
        if cpu is not None and memory is not None and 0 < cpu <= 65536 and 0 < memory <= 1048576:
            sizes.append({'cpu': cpu, 'memoryGiB': memory})
print(json.dumps({'all_correct': set(sys.argv[1:]).issubset(correct),
                  'returned_count': len(rows), 'cpu_mismatch_count': mismatched_cpu,
                  'memory_mismatch_count': mismatched_memory, 'observed_sizes': sizes}))
'''
    try:
        result = subprocess.run([*_split_python_command(h.args.python), "-c", code, *types],
                                cwd=h.server_cwd, env=h.server_env, capture_output=True,
                                text=True, encoding="utf-8", timeout=45, check=True)
        probe = json.loads(result.stdout)
        verified = probe.get("all_correct") is True
        for field in ("returned_count", "cpu_mismatch_count", "memory_mismatch_count"):
            count = probe.get(field)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= len(types):
                h.diagnostics["2c4g_sdk_" + field] = count
        sizes = probe.get("observed_sizes")
        if isinstance(sizes, list) and len(sizes) <= len(types):
            h.diagnostics["2c4g_sdk_observed_sizes"] = [
                {"cpu": row["cpu"], "memoryGiB": row["memoryGiB"]}
                for row in sizes if isinstance(row, dict)
                and type(row.get("cpu")) in {int, float} and 0 < row["cpu"] <= 65536
                and type(row.get("memoryGiB")) in {int, float} and 0 < row["memoryGiB"] <= 1048576
            ]
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        h.diagnostics["2c4g_sdk_probe_category"] = "sdk_error"
        verified = False
    else:
        h.diagnostics["2c4g_sdk_probe_category"] = (
            "wrong_real_sku" if not verified else "verified" if model_verified else "incomplete_model_verification"
        )
    h.diagnostics["2c4g_sdk_actual_types_correct"] = verified
    h.diagnostics["2c4g_independent_sdk_verified"] = verified
    return verified


def _verified_2c4g_instance_type(conclusion: Any, *, require_tool_evidence: bool = True) -> str:
    if not isinstance(conclusion, dict):
        return ""
    parameters = conclusion.get("deployment_parameters")
    checks = conclusion.get("hard_constraint_checks")
    if not isinstance(parameters, dict) or not isinstance(checks, list):
        return ""
    instance_type = parameters.get("InstanceType")
    if not isinstance(instance_type, str) or not instance_type:
        return ""

    expected = {"vcpu": (2.0, "count", "CpuCoreCount"), "memory": (4.0, "gib", "MemorySize")}
    matched: set[str] = set()
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "satisfied":
            continue
        constraint = check.get("constraint")
        parameter_values = check.get("parameter_values")
        if not isinstance(constraint, dict) or not isinstance(parameter_values, dict):
            continue
        property_name = str(constraint.get("property") or "").casefold()
        requirement = expected.get(property_name)
        if requirement is None or parameter_values.get("InstanceType") != instance_type:
            continue
        value, unit, result_field = requirement
        if _numeric(constraint.get("value")) != value or _numeric(check.get("actual_value")) != value:
            continue
        if str(check.get("actual_unit") or constraint.get("unit") or "").casefold() != unit:
            continue
        evidence = check.get("evidence")
        if require_tool_evidence and (not isinstance(evidence, list) or not any(
            isinstance(record, dict)
            and record.get("type") == "tool"
            and record.get("tool_name") == "aliyun_api"
            and str(record.get("action") or "").casefold() == "describeinstancetypes"
            and str(record.get("result_path") or "").endswith(result_field)
            and _numeric(record.get("actual_value")) == value
            for record in evidence
        )):
            continue
        matched.add(property_name)
    return instance_type if matched == set(expected) else ""


def _instance_type_result_is_2c4g(tool_results: list[dict[str, Any]], instance_type: str) -> bool:
    for item in tool_results:
        tool_input = item.get("input")
        if (
            item.get("toolName") != "aliyun_api"
            or item.get("isError") is not False
            or not isinstance(tool_input, dict)
            or str(tool_input.get("action") or "").casefold() != "describeinstancetypes"
        ):
            continue
        result = item.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                continue
        if not isinstance(result, (dict, list)):
            continue
        for record in _mapping_values(result):
            if (
                record.get("InstanceTypeId") == instance_type
                and _numeric(record.get("CpuCoreCount")) == 2
                and _numeric(record.get("MemorySize")) == 4
            ):
                return True
    # A successful completion can be accepted through LLM verification. A
    # truncated preview cannot prove the queried values, even with matching claims.
    return False


def _mapping_values(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _mapping_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _mapping_values(item)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_index(values: list[str], target: str) -> int | None:
    try:
        return values.index(target)
    except ValueError:
        return None


def run_image_initial(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream_image_text(
            text=args.initial_prompt,
            image_key="initial",
            name="01-initial-image",
            context_id="",
            task_id="",
        )
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial-image")
        h.checks["image initial reached step4 input_required"] = (
            initial.last_input_required_step_id == "confirm_and_select"
        )
        selection = h.stream(prompt=args.selection_prompt, name="02-select-candidate")
        selection = _finish_pipeline_after_possible_input(h, selection, args)
        h.checks["image initial selection completed pipeline"] = _pipeline_completed(selection)
        h.checks["image initial VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_image_ask_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=ASK_TRIGGER_PROMPT, name="01-ask-trigger", context_id="", task_id="")
        h.checks["initial reached input_required"] = _reached_input_required(initial)
        h.checks["input_required is ask_user_question"] = (
            _latest_pending_kind(h.run_dir / "01-ask-trigger.events.jsonl") == "ask_user_question"
        )
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot still waiting input"] = _snapshot_value(snapshot, "status") == "waiting_input"
        h.checks["pending input is ask_user_question"] = _pending_kind(snapshot) == "ask_user_question"
        answer = h.stream_image_text(
            text=ASK_FIRST_ANSWER,
            image_key="ask-first-answer",
            name="02-answer-first-ask-image",
            task_id="",
        )
        _add_hydrated_task_checks(h, answer, "first ask image answer")
        final_summary = answer
        if _waiting_for_followup_ask(h, answer):
            second = h.stream_image_text(
                text=ASK_SECOND_ANSWER,
                image_key="ask-second-answer",
                name="03-answer-second-ask-image",
            )
            _add_same_task_checks(h, second, "second ask image answer")
            _finish_pipeline_after_possible_input(h, second, args)
            final_summary = second
        else:
            _finish_pipeline_after_possible_input(h, answer, args)
        h.checks["pipeline completed after ask image recovery"] = _completed_snapshot_or_stream(h, final_summary)
        h.checks["deployment succeeded with Stack ID"] = _deployment_succeeded_with_stack_id(h.run_dir)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_image_selection_waiting(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial reached step4 input_required"] = initial.last_input_required_step_id == "confirm_and_select"
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot still waiting input"] = _snapshot_value(snapshot, "status") == "waiting_input"
        h.checks["pending input is confirm_and_select"] = _pending_step_id(snapshot) == "confirm_and_select"
        selection = h.stream_image_text(
            text=args.selection_prompt,
            image_key="selection",
            name="02-select-after-restart-image",
            task_id="",
        )
        _add_hydrated_task_checks(h, selection, "selection image answer")
        h.checks["selection image completed pipeline"] = _pipeline_completed(selection)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_image_normal_handoff(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        _complete_pipeline(h, args)
        _record_image_normal_checkpoint(h, "after_pipeline")
        _wait_completed_execution_release(h, timeout=min(20.0, args.event_timeout))
        normal = h.stream_image_text(
            text=args.normal_followup_prompt,
            image_key="normal-followup",
            name="03-normal-followup-image",
            task_id="",
        )
        h.checks["normal image follow-up stayed in same context"] = normal.context_id == h.context_id
        _record_image_normal_checkpoint(h, "after_normal_followup", normal)
        h.checks["normal image follow-up used a new task"] = (
            bool(normal.task_id) and normal.task_id != h.pipeline_task_id
            and h.diagnostics["image_normal_handoff_checkpoints"]["after_normal_followup"]
            ["control_state"].get("stream_task_matches") is True
        )
        h.checks["normal image follow-up finished turn"] = _normal_turn_finished(normal)
        h.checks["normal image follow-up produced text"] = bool(normal.text.strip())
        if (
            not h.checks["normal image follow-up used a new task"]
            or not h.checks["normal image follow-up finished turn"]
        ):
            raise RuntimeError("normal image follow-up did not finish with its own execution before restart")
        h.kill9_and_restart()
        _record_image_normal_checkpoint(h, "after_restart")
        h.snapshots["after_restart"] = h.fetch_state("after-restart")
        _add_completed_snapshot_checks(
            h.checks,
            "after-restart state",
            h.snapshots["after_restart"],
            context_id=h.context_id,
            task_id=h.pipeline_task_id,
        )
        recovery = h.stream(prompt=args.recovery_prompt, name="04-recovery-question", task_id="")
        h.checks["normal image recovery stayed in same context"] = recovery.context_id == h.context_id
        h.checks["normal image recovery finished turn"] = _normal_turn_finished(recovery)
        _record_image_normal_checkpoint(h, "after_recovery", recovery)

    return _run_with_harness(args, scenario, callback)


def _record_image_normal_checkpoint(h: ScenarioHarness, stage: str, summary: StreamSummary | None = None) -> None:
    """Observe the original sequence; never wait, retry, or alter its terminal decision."""
    checkpoints = h.diagnostics.setdefault("image_normal_handoff_checkpoints", {})
    control = _control_state_diagnostic(h.run_dir, h.context_id, h.pipeline_task_id)
    if summary and summary.task_id:
        control["stream_task_matches"] = _control_state_diagnostic(
            h.run_dir, h.context_id, summary.task_id
        ).get("task_matches")
    checkpoints[stage] = {
        "control_state": control,
        "a2a_states": summary.status_states[-12:] if summary else [],
        "terminal_markers": _terminal_markers([summary]) if summary else [],
    }


def _wait_completed_execution_release(h: ScenarioHarness, *, timeout: float) -> None:
    """A completed pipeline snapshot can precede its execution's durable release.

    Wait only for that original owner to finish. Never retry a user request,
    change the task identity, or interrupt work to manufacture a handoff.
    """
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        control = _control_state_diagnostic(h.run_dir, h.context_id, h.pipeline_task_id)
        polls += 1
        h.diagnostics["normal_handoff_wait_poll_count"] = polls
        ready = (control.get("present") is True and control.get("task_matches") is True
                 and control.get("phase") == "terminated" and control.get("release_ready") is True
                 and control.get("blocker_count") == 0 and control.get("active_subprocess_tools") == 0
                 and control.get("revision_settled") is True)
        h.diagnostics["normal_handoff_wait_completed"] = ready
        if ready:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("completed Pipeline execution did not release ownership before normal follow-up")
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def run_image_interrupt(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        h.failure_stage = "pre_rollback_candidate"
        initial = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        observed_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [initial],
            _candidate_started,
            description="candidate started before image interrupt",
            timeout=args.event_timeout,
            name_prefix="initial-running",
        )
        h.failure_stage = "rollback_completion"
        rollback = h.start_stream_image_text(
            text=ROLLBACK_PROMPT,
            caption=IMAGE_ROLLBACK_TARGET_CAPTION,
            image_key="rollback-interrupt",
            name="02-rollback-image-interrupt",
            prompt=IMAGE_INTERRUPT_PROMPT,
        )
        rollback_match = _wait_any(
            [*observed_streams, rollback],
            _event_type("rollback_completed"),
            description="image rollback_completed",
            timeout=args.event_timeout,
        )
        rollback_sequence = _matched_pipeline_sequence(rollback_match, "rollback_completed")
        h.diagnostics["rollback_fault_boundary_enforced"] = True
        streams_to_join = [*observed_streams, rollback]
        h.failure_stage = "post_rollback_step"
        _wait_any(
            [*observed_streams, rollback],
            _step_started("intent_parsing", after_sequence=rollback_sequence),
            description="post-image-rollback step_started(intent_parsing)",
            timeout=args.event_timeout,
        )
        h.diagnostics["post_rollback_fault_point_observed"] = True
        h.failure_stage = "restart"
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for stream in streams_to_join:
            _join_after_kill(stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after image interrupt restart"] = _snapshot(snapshot) is not None
        h.failure_stage = "resume"
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="03-continue-after-restart")
        h.current_goal = h._ci_owned_prompt(ROLLBACK_PROMPT)
        _finish_pipeline_after_possible_input(h, resumed, args, input_prompt=ROLLBACK_PROMPT)
        h.checks["pipeline completed after image interrupt recovery"] = _completed_snapshot_or_stream(h, resumed)
        h.failure_stage = "verify"
        final_state = h.fetch_state("after-image-interrupt-completion")
        _record_final_target_diagnostics(h, final_state)
        h.diagnostics["final_target_security_group"] = _has_any_marker(
            _final_deployment_evidence(final_state), SECURITY_GROUP_MARKERS
        )
        h.diagnostics["final_target_vswitch"] = _has_any_marker(
            _final_deployment_evidence(final_state), VSWITCH_MARKERS
        )
        _check_final_target_resource_types(h)

    return _run_with_harness(args, scenario, callback)


def run_rollback(args: argparse.Namespace, scenario: str) -> int:
    target_step = _ROLLBACK_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        h.failure_stage = "pre_rollback_candidate"
        initial = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        observed_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [initial],
            _candidate_started,
            description="candidate started before rollback",
            timeout=args.event_timeout,
            name_prefix="initial-running",
        )
        h.failure_stage = "rollback_completion"
        # This fixture phrase does not contain the harness's generic intent-change
        # markers. Supplemental questions must still use the new target after restart.
        # A generic architecture change legitimately returns to planning, not
        # necessarily intent parsing. The Step 1 crash case must request Step 1.
        # Later fault points remain milestones reached after normal replanning.
        rollback_prompt = (
            f"请先回退到 {target_step} 步骤。{ROLLBACK_PROMPT}"
            if target_step == "intent_parsing" else ROLLBACK_PROMPT
        )
        h.current_goal = h._ci_owned_prompt(rollback_prompt)
        rollback = h.start_stream(prompt=rollback_prompt, name="02-rollback-interrupt")
        rollback_match = _wait_any(
            [*observed_streams, rollback],
            _event_type("rollback_completed"),
            description="rollback_completed",
            timeout=args.event_timeout,
        )
        rollback_sequence = _matched_pipeline_sequence(rollback_match, "rollback_completed")
        h.diagnostics["rollback_fault_boundary_enforced"] = True
        streams_to_join = [*observed_streams, rollback]
        if target_step == "deploying":
            h.failure_stage = "post_rollback_confirmation"
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                streams_to_join,
                _input_required_step("confirm_and_select", after_sequence=rollback_sequence),
                description="post-rollback input_required(confirm_and_select)",
                timeout=args.event_timeout,
                name_prefix="post-rollback",
                answer_prompt=rollback_prompt,
                answer_input_steps={"intent_parsing"},
            )
            streams_to_join = observed_streams
            selection = h.start_stream(prompt=args.selection_prompt, name="03-select-after-rollback")
            streams_to_join.append(selection)
            h.failure_stage = "post_rollback_step"
            _wait_any(
                [selection],
                _step_started(target_step, after_sequence=rollback_sequence),
                description=f"post-rollback step_started({target_step})",
                timeout=args.event_timeout,
            )
        else:
            h.failure_stage = "post_rollback_step"
            streams_to_join = _wait_for_with_intervening_ask_inputs(
                h,
                streams_to_join,
                _step_started(target_step, after_sequence=rollback_sequence),
                description=f"post-rollback step_started({target_step})",
                timeout=args.event_timeout,
                name_prefix="post-rollback",
                answer_prompt=rollback_prompt,
                answer_input_steps={"intent_parsing"},
            )
        h.diagnostics["post_rollback_fault_point_observed"] = True
        h.failure_stage = "restart"
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for stream in streams_to_join:
            _join_after_kill(stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after rollback restart"] = _snapshot(snapshot) is not None
        h.failure_stage = "resume"
        resumed = h.stream(
            prompt=CONTINUE_PROMPT,
            name="04-continue-after-restart" if target_step == "deploying" else "03-continue-after-restart",
        )
        _finish_pipeline_after_possible_input(h, resumed, args, input_prompt=rollback_prompt)
        h.failure_stage = "verify"
        h.checks["pipeline completed after rollback recovery"] = _completed_snapshot_or_stream(h, resumed)
        final_state = h.fetch_state("after-rollback-completion")
        _record_final_target_diagnostics(h, final_state)
        _check_final_target_resource_types(h)

    return _run_with_harness(args, scenario, callback)


def run_cancel(args: argparse.Namespace, scenario: str) -> int:
    step_id = _CANCEL_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        if step_id == "deploying":
            initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
            initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
            h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
            stream = h.start_stream(prompt=args.selection_prompt, name="02-select-candidate-running")
        else:
            stream = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        if step_id == "evaluate_candidates":
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _candidate_started,
                description="candidate started before cancel",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        else:
            observed_streams = _wait_for_with_intervening_ask_inputs(
                h,
                [stream],
                _step_started(step_id),
                description=f"step_started({step_id})",
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        cancel_response = h.cancel_pipeline_task("cancel")
        h.checks["CancelTask returned response"] = isinstance(cancel_response, dict) and "error" not in cancel_response
        _wait_any_or_note(observed_streams, _status_state("TASK_STATE_CANCELED"), h, description="TASK_STATE_CANCELED")
        h.fetch_state("after-cancel")
        normal = h.stream(prompt=args.normal_followup_prompt, name="03-normal-after-cancel", task_id="")
        h.checks["normal chat after cancel stayed in same context"] = normal.context_id == h.context_id
        h.checks["normal chat after cancel finished"] = _normal_turn_finished(normal)
        h.kill9_and_restart()
        snapshot = h.fetch_state("after-restart")
        h.checks["snapshot remains canceled after restart"] = _snapshot_value(snapshot, "status") == "canceled"
        h.checks["normal chat after cancel persisted in session"] = _a2a_session_contains_user_message(
            h,
            args.normal_followup_prompt,
        )

    return _run_with_harness(args, scenario, callback)


def run_fault_after_snapshot(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        if not args.deterministic:
            raise RuntimeError("fault-after-snapshot requires --deterministic")
        h.current_goal = h._ci_owned_prompt(args.initial_prompt)
        initial = BackgroundStream(
            server_url=h.server_url,
            cwd=h.cwd,
            prompt=h.current_goal,
            context_id="",
            task_id="",
            name="01-initial-fault",
            run_dir=h.run_dir,
            timeout=h.args.stream_timeout,
            redaction_env=h.server_env,
        )
        h.summaries[initial.name] = initial.summary
        initial.start()
        _join_after_kill(initial, h)
        exit_code = h.wait_for_server_exit(expected_returncode=97, timeout=min(20.0, h.args.event_timeout))
        h.checks["deterministic fault exited with code 97"] = exit_code == 97
        h.disable_fault_injection()
        h.start_server()
        discovered = fetch_tasks(
            server_url=h.server_url,
            context_id="",
            run_dir=h.run_dir,
            name="after-restart-discovery",
            redaction_env=h.server_env,
        )
        h.snapshots["after-restart-discovery.task-list"] = discovered
        task_identity = _latest_task_identity(discovered)
        h.checks["initial stream captured contextId"] = bool(task_identity.get("contextId"))
        h.checks["initial stream captured pipeline taskId"] = bool(task_identity.get("taskId"))
        if not h.checks["initial stream captured contextId"] or not h.checks["initial stream captured pipeline taskId"]:
            raise RuntimeError("could not discover persisted task identity after deterministic restart")
        h.context_id = str(task_identity["contextId"])
        h.pipeline_task_id = str(task_identity["taskId"])
        h.snapshots["after_restart"] = h.fetch_state("after-restart")
        after_restart = h.capture_task_snapshots("after-restart")
        h.checks["task_get_after_restart"] = _task_response_matches(
            after_restart["task_get"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        )
        h.checks["task_list_after_restart"] = _task_list_contains(
            after_restart["task_list"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        )
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="02-continue-after-restart", task_id="")
        _add_hydrated_task_checks(h, resumed, "continue")
        _finish_pipeline_after_possible_input(h, resumed, args)
        after_continue = h.capture_task_snapshots("after-continue")
        h.checks["task_get_after_continue_completed"] = (
            _task_response_matches(
                after_continue["task_get"],
                task_id=h.pipeline_task_id,
                context_id=h.context_id,
            )
            and _task_status_state(after_continue["task_get"]) == "TASK_STATE_COMPLETED"
        )
        h.checks["task_list_after_continue_kept_recovered_task"] = _task_list_contains(
            after_continue["task_list"],
            task_id=h.pipeline_task_id,
            context_id=h.context_id,
        )
        h.checks["pipeline_completed"] = _completed_snapshot_or_stream(h, resumed)
        h.checks["created_vswitch"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_contract_graceful_success(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        _complete_pipeline(h, args)
        snapshot = h.fetch_state("contract-graceful-success")
        h.checks["graceful pipeline snapshot completed"] = _snapshot_value(snapshot, "status") == "completed"
        h.checks["graceful pipeline produced Aliyun evidence"] = _has_any_marker(
            _all_evidence(h),
            VSWITCH_MARKERS,
        )

    return _run_with_harness(args, scenario, callback)


def run_contract_graceful_cancel(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        stream = h.start_stream(
            prompt=args.initial_prompt,
            name="01-contract-cancel-running",
            context_id="",
            task_id="",
        )
        observed_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [stream],
            _step_started("intent_parsing"),
            description="step_started(intent_parsing)",
            timeout=args.event_timeout,
            name_prefix="contract-cancel",
        )
        _wait_for_contract_provider_request(h)
        cancel_response = h.cancel_pipeline_task("contract-cancel")
        h.checks["CancelTask returned response"] = isinstance(cancel_response, dict) and "error" not in cancel_response
        _wait_any_or_note(
            observed_streams,
            _status_state("TASK_STATE_CANCELED"),
            h,
            description="TASK_STATE_CANCELED",
        )
        _join_stream_or_note(stream, h)
        snapshot = h.fetch_state("contract-graceful-cancel")
        h.checks["graceful cancel persisted canceled state"] = _snapshot_value(snapshot, "status") == "canceled"

    return _run_with_harness(args, scenario, callback)


def _wait_for_contract_provider_request(h: ScenarioHarness) -> None:
    capture_value = h.server_env.get("IAC_CODE_E2E_PROVIDER_CAPTURE", "")
    if not capture_value:
        return
    capture_path = Path(capture_value)
    deadline = time.monotonic() + min(30.0, h.args.event_timeout)
    while time.monotonic() < deadline:
        if capture_path.exists() and capture_path.stat().st_size > 0:
            h.checks["provider request started before graceful cancel"] = True
            return
        time.sleep(0.05)
    h.checks["provider request started before graceful cancel"] = False
    raise TimeoutError("provider request did not start before graceful cancel")


def run_rollback_step5_cleanup(args: argparse.Namespace, scenario: str) -> int:
    return _run_rollback_step5_cleanup(args, scenario, kill_during_cleanup=False)


def run_rollback_step5_cleanup_recovery(args: argparse.Namespace, scenario: str) -> int:
    return _run_rollback_step5_cleanup(args, scenario, kill_during_cleanup=True)


def _run_rollback_step5_cleanup(
    args: argparse.Namespace,
    scenario: str,
    *,
    kill_during_cleanup: bool,
) -> int:
    def callback(h: ScenarioHarness) -> None:
        h.failure_stage = "initial_selection"

        initial = h.stream(
            prompt=args.initial_prompt,
            name="01-initial",
            context_id="",
            task_id="",
        )
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"

        h.failure_stage = "first_stack_create"
        first_deploy = h.start_stream(
            prompt=_cleanup_deployment_prompt(args.selection_prompt, h, "first"),
            name="02-create-first-stack",
        )
        first_stack_id = _wait_for_created_stack(
            first_deploy,
            exclude=set(),
            timeout=args.event_timeout,
        )
        h.checks["first rollback stack observed before rollback"] = bool(first_stack_id)

        h.failure_stage = "rollback_cleanup"
        rollback = h.start_stream(
            prompt=ROLLBACK_PROMPT,
            name="03-rollback-after-first-stack",
        )
        _wait_any(
            [first_deploy, rollback],
            _event_type("rollback_completed"),
            description="rollback_completed after first stack",
            timeout=args.event_timeout,
        )
        rollback_streams = _wait_for_with_intervening_ask_inputs(
            h, [first_deploy, rollback],
            _input_required_step("confirm_and_select"),
            description="post-rollback input_required(confirm_and_select)",
            timeout=_post_rollback_timeout(args),
            name_prefix="03-post-rollback",
        )
        cleanup_stack_ids = _cleanup_target_stack_ids(h, exclude=set())
        h.checks["rollback cleanup ledger includes first stack"] = bool(first_stack_id) and (
            first_stack_id in cleanup_stack_ids
        )
        h.checks["rollback cleanup target stacks observed"] = bool(cleanup_stack_ids)

        h.failure_stage = "second_stack_create"
        second_turns_before = set(h.summaries)
        second_prompt = _cleanup_deployment_prompt(args.selection_prompt, h, "second")
        second_deploy = h.start_stream(
            prompt=second_prompt,
            name="04-select-second-stack",
        )
        second_streams = _wait_for_with_intervening_ask_inputs(
            h, [second_deploy],
            _step_started("deploying"),
            description="second deployment step_started(deploying)",
            timeout=args.event_timeout,
            name_prefix="04-second-deployment",
            answer_input_steps={"confirm_and_select"},
            step_input_prompts={"confirm_and_select": second_prompt},
        )
        for stream in (*rollback_streams, *second_streams):
            _join_stream_or_note(stream, h)

        final_second = _finish_pipeline_after_possible_input(h, second_streams[-1].summary, args)
        h.checks["pipeline completed after second deployment"] = _completed_snapshot_or_stream(h, final_second)
        h.fetch_state("after-second-stack")
        second_ids = _unique_strings([
            _created_stack_id_from_stream(stream, exclude=set(cleanup_stack_ids)) for stream in second_streams
        ])
        second_ids = _unique_strings([*second_ids, *_created_stack_ids_in_turn_files(
            h.run_dir, set(h.summaries) - second_turns_before, exclude=set(cleanup_stack_ids),
        )])
        second_stack_id = second_ids[0] if second_ids else ""
        h.checks["second stack created after rollback"] = bool(second_stack_id)
        h.checks["second stack differs from first rollback stack"] = bool(second_stack_id) and (
            second_stack_id != first_stack_id
        )
        cleanup_stack_ids = _cleanup_target_stack_ids(
            h,
            exclude={stack_id for stack_id in [second_stack_id] if stack_id},
        )
        h.checks["rollback cleanup ledger includes first stack"] = bool(first_stack_id) and (
            first_stack_id in cleanup_stack_ids
        )
        h.checks["rollback cleanup target stacks observed"] = bool(cleanup_stack_ids)

        if kill_during_cleanup:
            h.failure_stage = "cleanup_recovery"
            cleanup_stream = h.start_stream(
                prompt=args.normal_followup_prompt,
                name="05-cleanup-running",
                task_id="",
            )
            _wait_for_cleanup_started(h, cleanup_stream, first_stack_id, timeout=args.event_timeout)
            h.kill9_and_restart()
            _join_after_kill(cleanup_stream, h)
            h.snapshots["after_cleanup_restart"] = h.fetch_state("after-cleanup-restart")
            cleanup_summary = h.stream(prompt=CLEANUP_RECOVERY_PROMPT, name="06-cleanup-after-restart", task_id="")
            h.checks["cleanup retriggered after restart"] = _events_file_has_cleanup_event(
                h.run_dir / "06-cleanup-after-restart.events.jsonl",
                stack_id=first_stack_id,
                event_types={"cleanup_started", "cleanup_progress", "cleanup_completed"},
            )
        else:
            h.failure_stage = "cleanup_normal_turn"
            cleanup_summary = h.stream(
                prompt=args.normal_followup_prompt,
                name="05-cleanup-normal-turn",
                task_id="",
            )
        h.checks["cleanup normal turn stayed in same context"] = cleanup_summary.context_id == h.context_id
        h.checks["cleanup normal turn used normal task"] = cleanup_summary.task_id != h.pipeline_task_id

        h.failure_stage = "cleanup_verify"
        # The normal turn can finish while ROS is still deleting the rollback
        # stack. Verify the same completion criteria after bounded polling.
        verify_deadline = time.monotonic() + min(args.event_timeout, 180.0)
        verify_attempt = 0
        ros_stack_ids = _unique_strings([*cleanup_stack_ids, second_stack_id])
        while True:
            verify_attempt += 1
            after_cleanup = h.fetch_state("after-cleanup")
            ros_states = _capture_ros_stack_states(h, ros_stack_ids, "after-cleanup")
            if bool(cleanup_stack_ids) and all(
                _cleanup_resource_completed(_cleanup_resource_for_stack(after_cleanup, stack_id))
                and _ros_stack_deleted(ros_states.get(stack_id, {}))
                for stack_id in cleanup_stack_ids
            ):
                break
            if time.monotonic() >= verify_deadline:
                break
            time.sleep(min(10.0, max(0.0, verify_deadline - time.monotonic())))
        h.snapshots["cleanup_verify_attempts"] = verify_attempt
        cleanup_resource = _cleanup_resource_for_stack(after_cleanup, first_stack_id)
        h.checks["first rollback stack cleanup completed in snapshot"] = _cleanup_resource_completed(cleanup_resource)
        h.checks["rollback cleanup stacks completed in snapshot"] = bool(cleanup_stack_ids) and all(
            _cleanup_resource_completed(_cleanup_resource_for_stack(after_cleanup, stack_id))
            for stack_id in cleanup_stack_ids
        )
        h.checks["cleanup snapshot does not target second stack"] = (
            bool(second_stack_id) and _cleanup_resource_for_stack(after_cleanup, second_stack_id) is None
        )

        h.checks["ROS first rollback stack deleted"] = _ros_stack_deleted(ros_states.get(first_stack_id, {}))
        h.checks["ROS rollback cleanup stacks deleted"] = bool(cleanup_stack_ids) and all(
            _ros_stack_deleted(ros_states.get(stack_id, {})) for stack_id in cleanup_stack_ids
        )
        h.checks["ROS second stack retained"] = bool(second_stack_id) and _ros_stack_retained(
            ros_states.get(second_stack_id, {})
        )
        if isinstance(getattr(h, "diagnostics", None), dict):
            h.diagnostics.update(
                _rollback_cleanup_diagnostics(
                    h, cleanup_summary, first_stack_id, cleanup_stack_ids, after_cleanup, ros_states
                )
            )

    return _run_with_harness(args, scenario, callback)


def _complete_pipeline(h: ScenarioHarness, args: argparse.Namespace) -> None:
    initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
    initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
    h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
    selection = h.stream(prompt=args.selection_prompt, name="02-select-candidate")
    # Selection may expose a legitimate parameter clarification or return a
    # refreshed selector. Drive those inputs before checking completion; an
    # input-required turn alone is not the final outcome of this scenario.
    selection = _finish_pipeline_after_possible_input(h, selection, args)
    h.checks["selection completed pipeline"] = _pipeline_completed(selection)
    h.checks["selection produced normal handoff"] = selection.normal_handoff_ready
    h.snapshots["after_pipeline"] = h.fetch_state("after-pipeline")


def _finish_pipeline_after_possible_input(
    h: ScenarioHarness,
    summary: StreamSummary,
    args: argparse.Namespace,
    *,
    input_prompt: str = CONTINUE_PROMPT,
) -> StreamSummary:
    current = summary
    for idx in range(1, 13):
        if _pipeline_completed(current):
            return current
        if current.last_status_state in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
            return current
        kind = _latest_pending_kind(h.run_dir / f"{current.name}.events.jsonl")
        if _reached_input_required(current) and kind == "ask_user_question":
            goal = getattr(h, "current_goal", "") or input_prompt
            response = _answer_pending_legacy_question(h, current, goal)
            current = h.stream(prompt=response, name=f"answer-after-resume-{idx}")
            continue
        if current.last_input_required_step_id == "confirm_and_select":
            current = h.stream(prompt=args.selection_prompt, name=f"select-after-resume-{idx}")
            continue
        if _reached_input_required(current):
            current = h.stream(prompt=input_prompt, name=f"continue-after-input-{idx}")
            continue
        if current.last_status_state in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
            return current
        snapshot = h.fetch_state(f"post-resume-{idx}")
        if _snapshot_value(snapshot, "status") == "completed":
            return current
        if (
            _snapshot_value(snapshot, "status") == "waiting_input"
            and _pending_step_id(snapshot) == "confirm_and_select"
        ):
            current = h.stream(prompt=args.selection_prompt, name=f"select-from-snapshot-{idx}")
            continue
        current = h.stream(prompt=input_prompt, name=f"continue-loop-{idx}")
    raise RuntimeError("pipeline remained pending after bounded supplemental inputs")


def _apply_event(summary: StreamSummary, payload: Any) -> None:
    summary.event_count += 1
    identity = _a2a_task_identity(payload)
    if identity is not None:
        summary.task_id = str(identity.get("taskId") or summary.task_id)
        summary.context_id = str(identity.get("contextId") or summary.context_id)
        state = identity.get("state")
        if state:
            summary.status_states.append(str(state))

    for envelope in _extract_pipeline_envelopes(payload):
        summary.task_id = str(envelope.get("taskId") or envelope.get("task_id") or summary.task_id)
        summary.context_id = str(envelope.get("contextId") or envelope.get("context_id") or summary.context_id)
        event_type = str(envelope.get("eventType") or envelope.get("event_type") or "")
        if event_type:
            summary.pipeline_event_types.append(event_type)
        if event_type == "input_required":
            step = envelope.get("step")
            if isinstance(step, dict):
                summary.last_input_required_step_id = str(step.get("id") or "")
        if _is_normal_handoff(envelope):
            summary.normal_handoff_ready = True

    status_texts = _status_message_texts(payload)
    if identity is not None and identity.get("state") in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
        summary.terminal_status_text = "".join(status_texts)
    for text in status_texts:
        summary.text += text


def _is_normal_handoff(envelope: dict[str, Any]) -> bool:
    data = envelope.get("data")
    return (
        isinstance(data, dict)
        and (envelope.get("eventType") or envelope.get("event_type")) == "pipeline_handoff_ready"
        and data.get("action") == "switch_to_normal"
        and (data.get("targetMode") or data.get("target_mode")) == "normal"
    )


def _pipeline_event_sequence(envelope: dict[str, Any]) -> int | None:
    value = envelope.get("sequence")
    if type(value) is int and value > 0:
        return value
    # Public A2A metadata is a Protobuf Struct: its numeric values round-trip
    # through JSON as doubles. Only accept exact positive integer sequences.
    if type(value) is float and 0 < value <= 2**53 - 1 and value.is_integer():
        return int(value)
    return None


def _matched_pipeline_sequence(match: EventMatch, event_type: str) -> int:
    sequences = [sequence for event in _extract_pipeline_envelopes(match.event)
                 if event.get("eventType") == event_type
                 and (sequence := _pipeline_event_sequence(event)) is not None]
    if not sequences:
        raise RuntimeError("rollback completion has no durable pipeline event sequence")
    return max(sequences)


def _step_started(step_id: str, *, after_sequence: int | None = None) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        return any(
            envelope.get("eventType") == "step_started"
            and isinstance(envelope.get("step"), dict)
            and envelope["step"].get("id") == step_id
            and (after_sequence is None or (
                (sequence := _pipeline_event_sequence(envelope)) is not None and sequence > after_sequence))
            for envelope in _extract_pipeline_envelopes(event)
        )

    return predicate


def _candidate_started(event: Any, _summary: StreamSummary) -> bool:
    return any(
        envelope.get("eventType") in {"candidate_started", "candidate_step_started"}
        for envelope in _extract_pipeline_envelopes(event)
    )


def _input_required_step(step_id: str, *, after_sequence: int | None = None) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        for envelope in _extract_pipeline_envelopes(event):
            if after_sequence is not None and (
                (sequence := _pipeline_event_sequence(envelope)) is None or sequence <= after_sequence
            ):
                continue
            step = envelope.get("step")
            data = envelope.get("data")
            data_step_id = data.get("stepId") if isinstance(data, dict) else None
            if envelope.get("eventType") == "input_required" and (
                (isinstance(step, dict) and step.get("id") == step_id) or data_step_id == step_id
            ):
                return True
        return False

    return predicate


def _event_type(event_type: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        return any(envelope.get("eventType") == event_type for envelope in _extract_pipeline_envelopes(event))

    return predicate


def _status_state(state: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        identity = _a2a_task_identity(event)
        return isinstance(identity, dict) and identity.get("state") == state

    return predicate


def _normal_text_started(event: Any, _summary: StreamSummary) -> bool:
    return bool(_status_message_texts(event))


def _wait_any(
    streams: Iterable[BackgroundStream],
    predicate: Callable[[Any, StreamSummary], bool],
    *,
    description: str,
    timeout: float,
) -> EventMatch:
    deadline = time.monotonic() + timeout
    last_error = ""
    active_streams = list(streams)
    while time.monotonic() < deadline:
        for stream in list(active_streams):
            try:
                return stream.wait_for(predicate, description=description, timeout=0.25)
            except TimeoutError:
                continue
            except RuntimeError as exc:
                last_error = str(exc)
                active_streams.remove(stream)
        if not active_streams:
            break
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {description}; last_error={last_error}")


def _wait_for_with_intervening_ask_inputs(
    h: ScenarioHarness,
    streams: Iterable[BackgroundStream],
    predicate: Callable[[Any, StreamSummary], bool],
    *,
    description: str,
    timeout: float,
    name_prefix: str,
    answer_prompt: str = INTERVENING_ASK_ANSWER,
    answer_input_steps: set[str] | None = None,
    step_input_prompts: dict[str, str] | None = None,
    progress_idle_timeout: float | None = None,
) -> list[BackgroundStream]:
    active_streams = list(streams)
    handled_finished_streams: set[int] = set()
    answered_count = 0
    answer_input_steps = set(answer_input_steps or ())
    deadline = time.monotonic() + timeout
    progress_at = time.monotonic()
    progress_offsets: dict[int, int] = {}
    progress_events: set[tuple[int, int | None, str, str]] = set()
    last_error = ""
    while time.monotonic() < deadline:
        for stream in list(active_streams):
            if progress_idle_timeout is not None:
                events = stream.events
                offset = progress_offsets.get(id(stream), 0)
                end = len(events)
                for event in events[offset:end]:
                    for envelope in _extract_pipeline_envelopes(event):
                        kind = envelope.get("eventType")
                        if kind not in {"step_started", "step_completed", "candidate_started", "candidate_completed",
                                        "candidate_step_started", "candidate_step_completed", "input_received",
                                        "tool_started", "tool_result"}:
                            continue
                        step = envelope.get("step")
                        marker = (id(stream), _pipeline_event_sequence(envelope), str(kind),
                                  str(step.get("id", "")) if isinstance(step, dict) else "")
                        if marker not in progress_events:
                            progress_events.add(marker)
                            progress_at = time.monotonic()
                progress_offsets[id(stream)] = end
                if time.monotonic() - progress_at >= progress_idle_timeout:
                    raise TimeoutError(f"no pipeline milestone progress while waiting for {description}")
            try:
                stream.wait_for(predicate, description=description, timeout=0.25)
                return active_streams
            except TimeoutError:
                continue
            except RuntimeError as exc:
                last_error = str(exc)
                stream_id = id(stream)
                if stream_id in handled_finished_streams:
                    continue
                handled_finished_streams.add(stream_id)
                kind = _latest_input_required_kind_from_events(stream.events)
                step_id = _latest_input_required_step_id_from_events(stream.events)
                should_answer = kind == "ask_user_question" or step_id in answer_input_steps
                if not should_answer:
                    continue
                answered_count += 1
                if answered_count > 4:
                    raise RuntimeError(f"too many intervening inputs before {description}") from exc
                input_name = "ask" if kind == "ask_user_question" else step_id
                h.notes.append(
                    f"answered intervening input_required({input_name}) while waiting for {description}: {stream.name}"
                )
                goal = (
                    answer_prompt if answer_prompt != INTERVENING_ASK_ANSWER
                    else getattr(h, "current_goal", "") or answer_prompt
                )
                response = (
                    _answer_pending_legacy_question(h, stream.summary, goal)
                    if kind == "ask_user_question" else (step_input_prompts or {}).get(step_id, answer_prompt)
                )
                answer = h.start_stream(
                    prompt=response,
                    name=f"{name_prefix}-answer-{input_name}-{answered_count}",
                )
                active_streams.append(answer)
                break
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {description}; last_error={last_error}")


def _wait_any_or_note(
    streams: Iterable[BackgroundStream],
    predicate: Callable[[Any, StreamSummary], bool],
    h: ScenarioHarness,
    *,
    description: str,
) -> None:
    try:
        _wait_any(streams, predicate, description=description, timeout=min(30.0, h.args.event_timeout))
    except Exception as exc:
        h.notes.append(f"did not observe {description}: {exc}")


def _wait_or_note(
    stream: BackgroundStream,
    predicate: Callable[[Any, StreamSummary], bool],
    h: ScenarioHarness,
    *,
    description: str,
) -> None:
    try:
        stream.wait_for(predicate, description=description, timeout=min(30.0, h.args.event_timeout))
    except Exception as exc:
        h.notes.append(f"did not observe {description}: {exc}")


def _latest_pending_input(path: Path) -> dict[str, Any]:
    pending: dict[str, Any] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pending
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for envelope in _extract_pipeline_envelopes(row):
            if envelope.get("eventType") == "input_required":
                pending = {**(envelope.get("data") or {}), **(envelope.get("input") or {})}
    return pending


def _answer_pending_legacy_question(h: ScenarioHarness, summary: StreamSummary, goal: str) -> str:
    pending = _latest_pending_input(h.run_dir / f"{summary.name}.events.jsonl")
    if not pending.get("question"):
        raise RuntimeError("pending clarification has no question text")
    counts = getattr(h, "question_counts", None)
    if not isinstance(counts, dict):
        counts = h.question_counts = {}
    diagnostics = getattr(h, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        diagnostics = h.diagnostics = {}
    config_dir = Path(h.server_env["IAC_CODE_CONFIG_DIR"])
    response, _ = answer_question(config_dir, pending, case_facts(goal, getattr(h, "network_fixture_facts", {})),
                                  counts, diagnostics,
                                  conversation=question_conversation(h))
    return response


def _answer_intervening_ask_inputs(
    h: ScenarioHarness,
    summary: StreamSummary,
    *,
    name_prefix: str,
    answer_prompt: str = INTERVENING_ASK_ANSWER,
) -> StreamSummary:
    current = summary
    for idx in range(1, 5):
        if _pipeline_completed(current):
            return current
        if not _reached_input_required(current):
            return current
        kind = _latest_pending_kind(h.run_dir / f"{current.name}.events.jsonl")
        if kind != "ask_user_question":
            return current
        h.notes.append(f"answered intervening ask_user_question before step4 selection: {current.name}")
        goal = (
            answer_prompt if answer_prompt != INTERVENING_ASK_ANSWER
            else getattr(h, "current_goal", "")
            or getattr(getattr(h, "args", None), "initial_prompt", answer_prompt)
        )
        response = _answer_pending_legacy_question(h, current, goal)
        current = h.stream(prompt=response, name=f"{name_prefix}-answer-ask-{idx}")
    return current


def _waiting_for_followup_ask(h: ScenarioHarness, summary: StreamSummary) -> bool:
    if not summary.last_input_required_step_id:
        return False
    events_path = h.run_dir / f"{summary.name}.events.jsonl"
    return _latest_pending_kind(events_path) == "ask_user_question"


def _join_after_kill(stream: BackgroundStream, h: ScenarioHarness) -> None:
    try:
        stream.join(timeout=10)
    except Exception as exc:
        h.notes.append(f"{stream.name} ended after kill with: {type(exc).__name__}: {exc}")


def _reached_input_required(summary: StreamSummary) -> bool:
    return "TASK_STATE_INPUT_REQUIRED" in summary.status_states or "input_required" in summary.pipeline_event_types


def _pipeline_completed(summary: StreamSummary) -> bool:
    return summary.last_status_state == "TASK_STATE_COMPLETED" or "pipeline_completed" in summary.pipeline_event_types


def _selection_advanced_past_waiting_step(summary: StreamSummary) -> bool:
    event_types = summary.pipeline_event_types
    try:
        received_index = event_types.index("input_received")
        completed_index = event_types.index("step_completed", received_index + 1)
    except ValueError:
        return False
    return "step_started" in event_types[completed_index + 1 :] or _pipeline_completed(summary)


def _backup_delay_control_path(h: ScenarioHarness) -> Path:
    raw = h.server_env.get("IAC_CODE_E2E_BACKUP_DELAY_CONTROL", "")
    if not raw:
        raise RuntimeError("backup delay control path is not configured")
    return Path(raw)


def _backup_delay_marker_path(control: Path, marker: str) -> Path:
    return control.with_name(f"{control.name}.{marker}.json")


def _wait_for_backup_delay_marker(control: Path, marker: str, *, timeout: float) -> dict[str, Any]:
    path = _backup_delay_marker_path(control, marker)
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.05)
            continue
        if isinstance(value, dict):
            return value
        last_error = "marker was not a JSON object"
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for backup delay marker {path}: {last_error}")


def _wait_for_backup_start_with_intervening_asks(
    h: ScenarioHarness, control: Path, initial_stream: BackgroundStream, *, timeout: float
) -> tuple[dict[str, Any], list[BackgroundStream]]:
    """Keep Step 1 clarification turns moving while waiting for the Step 4 backup hook."""

    streams = [initial_stream]
    handled: set[int] = set()
    path = _backup_delay_marker_path(control, "started")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return _wait_for_backup_delay_marker(control, "started", timeout=1.0), streams
        for stream in list(streams):
            if not stream.done or id(stream) in handled:
                continue
            handled.add(id(stream))
            kind = _latest_input_required_kind_from_events(stream.events)
            if kind != "ask_user_question":
                if all(item.done for item in streams):
                    raise RuntimeError("initial A2A stream ended before backup delay without a clarification question")
                continue
            if len(streams) > 4:
                raise RuntimeError("too many intervening questions before backup delay")
            h.notes.append(f"answered intervening ask_user_question before backup delay: {stream.name}")
            response = _answer_pending_legacy_question(h, stream.summary, h.current_goal)
            streams.append(
                h.start_stream(
                    prompt=response,
                    name=f"01-initial-answer-ask-{len(streams)}",
                )
            )
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for backup delay to start after Step 1 clarification")


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _add_same_task_checks(h: ScenarioHarness, summary: StreamSummary, prefix: str) -> None:
    h.checks[f"{prefix} requested recovered taskId"] = summary.request_task_id == h.pipeline_task_id
    h.checks[f"{prefix} stayed in recovered context"] = summary.context_id == h.context_id
    h.checks[f"{prefix} streamed recovered taskId"] = summary.task_id == h.pipeline_task_id


def _add_hydrated_task_checks(h: ScenarioHarness, summary: StreamSummary, prefix: str) -> None:
    h.checks[f"{prefix} omitted taskId"] = summary.request_task_id == ""
    h.checks[f"{prefix} stayed in recovered context"] = summary.context_id == h.context_id
    h.checks[f"{prefix} hydrated recovered taskId"] = summary.task_id == h.pipeline_task_id


def _has_already_working_error(summary: StreamSummary) -> bool:
    return "Task is already working" in summary.text


def _has_terminal_state_error(summary: StreamSummary) -> bool:
    return "terminal state" in summary.text


def _waiting_input_backup_snapshots(h: ScenarioHarness) -> dict[str, Any]:
    backup_root = getattr(h, "backup_root", None)
    if backup_root is None:
        _append_harness_note(h, "cannot inspect backup snapshots: backup root is not configured")
        return {"error": "backup root is not configured"}
    cwd, session_id = _pipeline_session_identity(h)
    if not cwd or not session_id:
        _append_harness_note(h, "cannot inspect backup snapshots: missing cwd/session_id")
        return {"error": "missing cwd/session_id"}

    storage = SessionStorage(projects_dir=Path(backup_root) / "projects")
    deadline = time.monotonic() + 10.0
    last_error = ""
    while time.monotonic() < deadline:
        try:
            session_dir = storage.v2_session_dir(cwd, session_id)
            if session_dir is None:
                last_error = "backup session directory not found"
            else:
                task_path = session_dir / "a2a" / "task.json"
                context_path = session_dir / "a2a" / "context.json"
                task = json.loads(task_path.read_text(encoding="utf-8"))
                context = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(task, dict) and isinstance(context, dict):
                    return {
                        "sessionDir": str(session_dir),
                        "task": _redact_json_value(task, h.server_env),
                        "context": _redact_json_value(context, h.server_env),
                    }
                last_error = "backup snapshots are not JSON objects"
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)

    _append_harness_note(h, f"cannot inspect backup snapshots: {last_error}")
    return {"error": last_error}


def _remove_primary_session_for_backup_restore(h: ScenarioHarness) -> dict[str, Any]:
    backup_root = getattr(h, "backup_root", None)
    if backup_root is None:
        raise RuntimeError("backup root is not configured")
    cwd, session_id = _pipeline_session_identity(h)
    if not cwd or not session_id:
        raise RuntimeError("cannot remove primary session: missing cwd/session_id")

    primary_storage = SessionStorage()
    backup_storage = SessionStorage(projects_dir=Path(backup_root) / "projects")
    primary_session_dir = primary_storage.v2_session_dir(cwd, session_id)
    backup_session_dir = backup_storage.v2_session_dir(cwd, session_id)
    if primary_session_dir is None or not primary_session_dir.is_dir():
        raise RuntimeError("primary session directory not found")
    if backup_session_dir is None or not backup_session_dir.is_dir():
        raise RuntimeError("backup session directory not found")
    if primary_session_dir.is_symlink() or backup_session_dir.is_symlink():
        raise RuntimeError("refusing to remove session through a symlink")

    primary_projects_dir = get_projects_dir().resolve()
    backup_projects_dir = (Path(backup_root) / "projects").resolve()
    primary_resolved = primary_session_dir.resolve()
    backup_resolved = backup_session_dir.resolve()
    if primary_resolved.name != session_id or primary_resolved == primary_projects_dir:
        raise RuntimeError("refusing to remove an unexpected primary session path")
    if primary_projects_dir not in primary_resolved.parents:
        raise RuntimeError("primary session resolves outside the active projects directory")
    if backup_projects_dir not in backup_resolved.parents:
        raise RuntimeError("backup session resolves outside the configured backup directory")
    if (
        primary_resolved == backup_resolved
        or backup_resolved in primary_resolved.parents
        or primary_resolved in backup_resolved.parents
    ):
        raise RuntimeError("primary session path overlaps the backup session path")

    primary_session_file = primary_storage.session_path(cwd, session_id)
    evidence = {
        "cwd": cwd,
        "sessionId": session_id,
        "primaryProjectsDir": str(primary_projects_dir),
        "primarySessionDir": str(primary_resolved),
        "primarySessionFile": str(primary_session_file),
        "backupSessionDir": str(backup_resolved),
        "primaryExistedBeforeRemoval": True,
        "backupExistedBeforeRemoval": True,
    }
    shutil.rmtree(primary_session_dir)
    evidence["primaryRemovedBeforeRestart"] = not primary_session_dir.exists()
    evidence["backupPresentAfterRemoval"] = backup_session_dir.is_dir()
    if not evidence["primaryRemovedBeforeRestart"] or not evidence["backupPresentAfterRemoval"]:
        raise RuntimeError("failed to establish backup-only restore preconditions")
    _write_json(h.run_dir / "step4.backup-only-restore.json", evidence)
    return evidence


def _completed_snapshot_or_stream(h: ScenarioHarness, summary: StreamSummary) -> bool:
    if _pipeline_completed(summary):
        return True
    snapshot = h.fetch_state("completion-check")
    return _snapshot_value(snapshot, "status") == "completed"


def _jsonrpc_post(
    *,
    server_url: str,
    payload: dict[str, Any],
    run_dir: Path,
    name: str,
    suffix: str,
    redaction_env: dict[str, str] | None = None,
) -> Any:
    _append_jsonl(
        run_dir / "requests.jsonl",
        {"name": name, "payload": payload, "at": _utc_now()},
        redaction_env,
    )
    artifact: dict[str, Any] = {"request": _redact_json_value(payload, redaction_env)}
    request = Request(
        server_url.rstrip("/") + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else None
            artifact["http_status"] = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = {"body": _redact_sensitive_text(raw, redaction_env)}
        artifact["http_status"] = exc.code
        artifact["error"] = f"HTTP {exc.code}"
    except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
        data = {"error": str(exc)}
        artifact["http_status"] = 0
        artifact["error"] = str(exc)
    artifact["response"] = _redact_json_value(data, redaction_env)
    _write_json(run_dir / f"{name}.{suffix}.json", artifact)
    return artifact


def fetch_task(
    server_url: str,
    task_id: str,
    run_dir: Path,
    name: str,
    redaction_env: dict[str, str] | None,
    history_length: int | None = None,
) -> Any:
    payload = build_task_get_payload(task_id=task_id, history_length=history_length, request_id=str(uuid.uuid4()))
    return _jsonrpc_post(
        server_url=server_url,
        payload=payload,
        run_dir=run_dir,
        name=name,
        suffix="task-get",
        redaction_env=redaction_env,
    )


def fetch_tasks(
    server_url: str,
    context_id: str,
    run_dir: Path,
    name: str,
    redaction_env: dict[str, str] | None,
) -> Any:
    params: dict[str, Any] = {"includeArtifacts": False}
    if context_id:
        params["contextId"] = context_id
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "ListTasks",
        "params": params,
    }
    return _jsonrpc_post(
        server_url=server_url,
        payload=payload,
        run_dir=run_dir,
        name=name,
        suffix="task-list",
        redaction_env=redaction_env,
    )


def _fetch_pipeline_state_for_redaction_audit(h: ScenarioHarness) -> dict[str, Any]:
    """Read the public state in memory; never persist its credential values."""

    query = urlencode({"contextId": h.context_id, "taskId": h.pipeline_task_id})
    request = Request(h.server_url.rstrip("/") + f"/iac-code/pipeline/state?{query}", method="GET")
    with urlopen(request, timeout=30) as response:
        raw = response.read().decode("utf-8", errors="strict")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("public pipeline state was not a JSON object")
    return value


def _load_canonical_pipeline_snapshot(h: ScenarioHarness) -> dict[str, Any]:
    """Load the functional snapshot in memory without copying secrets to run artifacts."""

    cwd, session_id = _pipeline_session_identity(h)
    if not cwd or not session_id:
        raise RuntimeError("cannot locate canonical pipeline snapshot: missing cwd/session id")
    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    snapshot_path = pipeline_dir / "a2a-snapshot.json"
    try:
        value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read canonical pipeline snapshot: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("canonical pipeline snapshot was not a JSON object")
    return value


def _build_step4_redaction_audit(
    canonical_snapshot: dict[str, Any],
    public_snapshot: dict[str, Any],
    *,
    known_server_paths: Iterable[str],
    safe_mode: str,
    canonical_token_events: dict[str, Any] | None = None,
    public_token_events: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare step4 data while returning only non-secret evidence."""

    canonical_credentials = _credential_scalar_values(canonical_snapshot)
    public_credentials = _credential_scalar_values(public_snapshot)
    canonical_tokens = _token_counter_values(canonical_snapshot)
    public_tokens = _token_counter_values(public_snapshot)
    canonical_tokens.update(canonical_token_events or {})
    public_tokens.update(public_token_events or {})
    canonical_parameter_placeholders = _functional_parameter_placeholder_paths(canonical_snapshot)
    public_parameter_placeholders = _functional_parameter_placeholder_paths(public_snapshot)
    known_paths = tuple(dict.fromkeys(path for path in known_server_paths if isinstance(path, str) and path))
    public_text = json.dumps(public_snapshot, ensure_ascii=False, default=str)

    credential_paths = set(canonical_credentials)
    public_credential_paths = set(public_credentials)
    token_paths = set(canonical_tokens)
    public_token_paths = set(public_tokens)
    return {
        "safeModeValue": safe_mode,
        "canonicalCredentialFieldCount": len(canonical_credentials),
        "publicCredentialFieldCount": len(public_credentials),
        "credentialFieldPaths": sorted(credential_paths),
        "credentialFieldsMissingFromPublic": sorted(credential_paths - public_credential_paths),
        "unexpectedPublicCredentialFields": sorted(public_credential_paths - credential_paths),
        "credentialFieldsChangedInPublic": sorted(
            path
            for path in credential_paths & public_credential_paths
            if canonical_credentials[path] != public_credentials[path]
        ),
        "canonicalFunctionalParameterPlaceholderPaths": canonical_parameter_placeholders,
        "publicFunctionalParameterPlaceholderPaths": public_parameter_placeholders,
        "canonicalTokenCounterCount": len(canonical_tokens),
        "publicTokenCounterCount": len(public_tokens),
        "canonicalNonNumericTokenCounterPaths": sorted(
            path for path, value in canonical_tokens.items() if not _is_number(value)
        ),
        "publicNonNumericTokenCounterPaths": sorted(
            path for path, value in public_tokens.items() if not _is_number(value)
        ),
        "tokenCounterPathsMissingFromPublic": sorted(token_paths - public_token_paths),
        "unexpectedPublicTokenCounterPaths": sorted(public_token_paths - token_paths),
        "tokenCountersChangedInPublic": sorted(
            path for path in token_paths & public_token_paths if canonical_tokens[path] != public_tokens[path]
        ),
        "canonicalKnownServerPathOccurrences": _known_server_path_occurrences(canonical_snapshot, known_paths),
        "publicKnownServerPathOccurrences": _known_server_path_occurrences(public_snapshot, known_paths),
        "publicPathPlaceholderOccurrences": public_text.count("[PATH]"),
    }


def _step4_redaction_checks(audit: dict[str, Any]) -> dict[str, bool]:
    return {
        "A2A safe mode is enabled": str(audit.get("safeModeValue") or "").strip().lower() in {"1", "true", "yes", "on"},
        "canonical snapshot contains generated credential parameters": bool(audit.get("canonicalCredentialFieldCount")),
        "canonical functional parameters contain no redaction placeholders": not bool(
            audit.get("canonicalFunctionalParameterPlaceholderPaths")
        ),
        "public functional parameters contain no redaction placeholders": not bool(
            audit.get("publicFunctionalParameterPlaceholderPaths")
        ),
        "public credential fields match canonical values": not any(
            audit.get(key)
            for key in (
                "credentialFieldsMissingFromPublic",
                "unexpectedPublicCredentialFields",
                "credentialFieldsChangedInPublic",
            )
        ),
        "canonical token counters are numeric when present": not bool(
            audit.get("canonicalNonNumericTokenCounterPaths")
        ),
        "public token counters remain numeric and unchanged when present": not any(
            audit.get(key)
            for key in (
                "publicNonNumericTokenCounterPaths",
                "tokenCounterPathsMissingFromPublic",
                "unexpectedPublicTokenCounterPaths",
                "tokenCountersChangedInPublic",
            )
        ),
        "canonical snapshot contains a known server path": bool(audit.get("canonicalKnownServerPathOccurrences")),
        "safe mode hides known server paths from public state": audit.get("publicKnownServerPathOccurrences") == 0,
        "safe mode public state contains a path placeholder": bool(audit.get("publicPathPlaceholderOccurrences")),
    }


def _record_redaction_candidate_diagnostics(h: Any, snapshot: dict[str, Any]) -> None:
    """Export only counts from retained intent and architecture, never their text."""
    steps = snapshot.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("conclusion"), dict):
            continue
        conclusion = step["conclusion"]
        if step.get("id") == "intent_parsing":
            requested = conclusion.get("requested_candidate_count")
            h.diagnostics["redaction_intent_structured_candidate_count"] = (
                requested if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0 else 0
            )
            text = " ".join(conclusion.get(key, "") for key in ("additional_notes", "user_message_summary")
                            if isinstance(conclusion.get(key), str))
            numbers = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            markers = re.findall(r"(?<![0-9])(10|[1-9一二两三四五六七八九十])\s*个?\s*(?:方案|候选)", text)
            counts = {int(value) if value.isdigit() else numbers[value] for value in markers}
            # Zero means no unique recognized marker; it cannot prove the intent omitted a paraphrased requirement.
            h.diagnostics["redaction_intent_requested_candidate_count"] = counts.pop() if len(counts) == 1 else 0
        elif step.get("id") == "architecture_planning":
            candidates = conclusion.get("candidates")
            h.diagnostics["redaction_architecture_candidate_count"] = (
                len(candidates) if isinstance(candidates, list) else 0
            )


def _record_noecho_redaction_diagnostics(h: Any, snapshot: dict[str, Any]) -> None:
    if not isinstance(getattr(h, "diagnostics", None), dict):
        h.diagnostics = {}
    names: set[str] = set()
    templates = []
    for item in _ordered_tool_results(snapshot):
        if item.get("isError") is not False or not isinstance(item.get("input"), dict):
            continue
        tool_input = item["input"]
        text = tool_input.get("content") if item.get("toolName") == "write_file" else None
        conclusion = tool_input.get("conclusion")
        if item.get("toolName") == "complete_step" and isinstance(conclusion, dict):
            text = conclusion.get("template")
        if isinstance(text, str) and len(text) <= 2_000_000:
            try:
                template = yaml.safe_load(text)
            except yaml.YAMLError:
                continue
            if isinstance(template, dict):
                templates.append(template)
    for template in templates:
        parameters = template.get("Parameters")
        if isinstance(parameters, dict):
            names.update(name for name, spec in parameters.items()
                         if isinstance(name, str) and isinstance(spec, dict)
                         and spec.get("NoEcho") in (True, "true", "True"))
    h.diagnostics["redaction_noecho_parameter_count"] = len(names)
    h.diagnostics["redaction_noecho_non_password_name_count"] = sum("password" not in n.casefold() for n in names)
    h.diagnostics["redaction_noecho_parameter_value_count"] = sum(
        key in names and isinstance(item, str) and bool(item.strip())
        for _path, key, item in _iter_scalar_values(snapshot))


def _credential_scalar_values(value: Any) -> dict[str, Any]:
    return {path: item for path, key, item in _iter_scalar_values(value) if "password" in key.casefold()}


def _token_counter_values(value: Any) -> dict[str, Any]:
    """Read snapshot usage only, not similarly named cloud request/schema fields."""
    display = value.get("display") if isinstance(value, dict) else None
    usage = display.get("usage") if isinstance(display, dict) else None
    return {
        "display.usage." + key: item for key, item in (usage or {}).items()
        if key in _USAGE_TOKEN_FIELDS
    } if isinstance(usage, dict) else {}


_USAGE_TOKEN_FIELDS = {
    "totalTokens", "inputTokens", "outputTokens", "cachedInputTokens", "systemPromptTokens",
    "toolDefinitionTokens", "userMessageTokens", "assistantMessageTokens", "toolResultTokens",
    "originalTokens", "compactedTokens",
}
_USAGE_EVENT_TYPES = {
    "usage", "context_usage", "context_compaction_started", "context_compacted", "context_compaction_failed",
}


def _usage_event_token_counters(payload: Any) -> dict[str, Any]:
    """Correlate genuine counter events by immutable event ID, not tool output text."""
    counters: dict[str, Any] = {}
    for envelope in _extract_pipeline_envelopes(payload):
        if envelope.get("eventType") not in _USAGE_EVENT_TYPES:
            continue
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        event_id = envelope.get("eventId")
        if not isinstance(event_id, str) or not event_id:
            raise RuntimeError("usage event is missing its correlation identity")
        fields = {"events." + event_id + "." + key: value for key, value in data.items()
                  if key in _USAGE_TOKEN_FIELDS}
        _merge_usage_token_counters(counters, fields)
    return counters


def _merge_usage_token_counters(target: dict[str, Any], observed: dict[str, Any]) -> None:
    for key, value in observed.items():
        # An inconsistent replay must fail rather than overwrite earlier evidence.
        if key in target and (target[key] != value or _is_number(target[key]) != _is_number(value)):
            target[key] = "inconsistent_usage_counter"
        else:
            target[key] = value


def _load_canonical_usage_token_counters(h: ScenarioHarness) -> dict[str, Any]:
    cwd, session_id = _pipeline_session_identity(h)
    if not cwd or not session_id:
        raise RuntimeError("cannot locate canonical usage events")
    pipeline_dir = existing_a2a_pipeline_dir_for_session(cwd=cwd, session_id=session_id)
    journal = A2APipelineJournal(pipeline_dir)
    if not journal.path.is_file():
        raise RuntimeError("canonical usage journal is missing")
    counters: dict[str, Any] = {}
    for envelope in journal.read_all_strict():
        _merge_usage_token_counters(counters, _usage_event_token_counters(envelope))
    return counters


def _functional_parameter_placeholder_paths(value: Any) -> list[str]:
    paths: list[str] = []
    for path, key, item in _iter_scalar_values(value):
        if not isinstance(item, str) or item.strip().casefold() not in {
            placeholder.casefold() for placeholder in REDACTION_PLACEHOLDERS
        }:
            continue
        segments = tuple(segment.casefold() for segment in _json_path_segments(path))
        if (
            "password" in key.casefold()
            or "deployment_parameters" in segments
            or ("preview_validation" in segments and "parameters" in segments)
        ):
            paths.append(path)
    return sorted(paths)


def _known_server_path_occurrences(value: Any, known_server_paths: Iterable[str]) -> int:
    """Count path tokens under known roots without matching sibling path prefixes."""

    roots = tuple({"path": path, "label": "[PATH]"} for path in known_server_paths if path)
    if not roots:
        return 0

    def count_text(text: str) -> int:
        redacted = redact_known_public_paths(text, roots)
        return max(0, redacted.count("[PATH]") - text.count("[PATH]"))

    def visit(item: Any) -> int:
        if isinstance(item, dict):
            return sum(
                (count_text(key) if isinstance(key, str) else 0) + visit(child) for key, child in item.items()
            )
        if isinstance(item, (list, tuple)):
            return sum(visit(child) for child in item)
        if isinstance(item, str):
            return count_text(item)
        return 0

    return visit(value)


def _iter_scalar_values(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            next_path = (*path, key)
            if isinstance(item, (dict, list, tuple)):
                yield from _iter_scalar_values(item, next_path)
            else:
                yield _json_path(next_path), key, item
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            next_path = (*path, str(index))
            if isinstance(item, (dict, list, tuple)):
                yield from _iter_scalar_values(item, next_path)
            else:
                yield _json_path(next_path), str(index), item


def _json_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _json_path_segments(path: str) -> tuple[str, ...]:
    return tuple(path.split(".")) if path else ()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _snapshot(response: Any) -> dict[str, Any] | None:
    snapshot = response.get("snapshot") if isinstance(response, dict) else None
    return snapshot if isinstance(snapshot, dict) else None


def _jsonrpc_result(response: Any) -> Any:
    if not isinstance(response, dict):
        return None
    payload = response.get("response")
    if not isinstance(payload, dict):
        return None
    return payload.get("result")


def _task_response_matches(response: Any, *, task_id: str, context_id: str) -> bool:
    result = _jsonrpc_result(response)
    identity = _a2a_task_identity(result)
    return isinstance(identity, dict) and identity.get("taskId") == task_id and identity.get("contextId") == context_id


def _task_status_state(response: Any) -> str:
    result = _jsonrpc_result(response)
    status = result.get("status") if isinstance(result, dict) else None
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else ""


def _task_list_contains(response: Any, *, task_id: str, context_id: str) -> bool:
    result = _jsonrpc_result(response)
    tasks = result.get("tasks") if isinstance(result, dict) else None
    if not isinstance(tasks, list):
        return False
    for task in tasks:
        identity = _a2a_task_identity(task)
        if isinstance(identity, dict) and identity.get("taskId") == task_id and identity.get("contextId") == context_id:
            return True
    return False


def _latest_task_identity(response: Any) -> dict[str, Any]:
    result = _jsonrpc_result(response)
    tasks = result.get("tasks") if isinstance(result, dict) else None
    if not isinstance(tasks, list):
        return {}
    for task in tasks:
        identity = _a2a_task_identity(task)
        if isinstance(identity, dict) and identity.get("taskId") and identity.get("contextId"):
            return identity
    return {}


def _snapshot_value(response: Any, key: str) -> Any:
    snapshot = _snapshot(response)
    return snapshot.get(key) if snapshot is not None else None


def _step_evidence(response: Any, step_id: str) -> str:
    snapshot = _snapshot(response)
    steps = snapshot.get("steps") if isinstance(snapshot, dict) else None
    if not isinstance(steps, list):
        return ""
    matches = [
        step
        for step in steps
        if isinstance(step, dict)
        and step.get("id") == step_id
        and step.get("status") in {"completed", "working", "waiting_input"}
    ]
    if not matches:
        return ""
    return json.dumps(matches[-1], ensure_ascii=False, default=str)


def _record_final_target_diagnostics(h: Any, response: Any) -> None:
    diagnostics = getattr(h, 'diagnostics', None)
    if not isinstance(diagnostics, dict):
        diagnostics = h.diagnostics = {}
    if getattr(getattr(h, 'args', None), 'allow_real_cloud', False):
        diagnostics.update(_native_final_target_resource_facts(h, response))
    step = _step_evidence(response, 'deploying')
    context = _handoff_context(response) or {}
    snapshot = _snapshot(response)
    normal = snapshot.get('normalHandoff') if isinstance(snapshot, dict) else None
    diagnostics['final_target_handoff_present'] = isinstance(normal, dict)
    diagnostics['final_target_context_present'] = bool(context)
    selected = context.get('selected_plan')
    diagnostics['final_target_selected_plan_present'] = isinstance(selected, dict) and bool(selected)
    if isinstance(selected, dict):
        diagnostics['final_target_selected_plan_fields'] = sorted(
            set(selected).intersection(FINAL_TARGET_EVIDENCE_KEYS | FINAL_SELECTED_PLAN_REALIZED_KEYS))
        result = selected.get('selected_candidate_result')
        if isinstance(result, dict):
            diagnostics['final_target_candidate_result_fields'] = sorted(
                set(result).intersection(FINAL_CANDIDATE_RESULT_EVIDENCE_KEYS))
            template = result.get('template')
            diagnostics['final_target_template_body_present'] = (
                isinstance(template, str) and bool(template)
                or isinstance(template, dict) and isinstance(template.get('template'), str)
                and bool(template['template'])
            )
            body = template.get('template') if isinstance(template, dict) else template
            parsed = None
            if isinstance(body, str) and len(body) <= 1_000_000:
                try:
                    parsed = yaml.safe_load(body)
                except yaml.YAMLError:
                    pass
            resources = parsed.get('Resources') if isinstance(parsed, dict) else None
            diagnostics['final_target_template_resources_inspected'] = isinstance(resources, dict)
            for target, resource_type in (
                ('security_group', 'ALIYUN::ECS::SecurityGroup'), ('vswitch', 'ALIYUN::ECS::VSwitch'),
            ):
                diagnostics['final_target_template_' + target] = any(
                    isinstance(resource, dict) and resource.get('Type') == resource_type
                    for resource in resources.values()
                ) if isinstance(resources, dict) else False
    handoff = json.dumps({
        'selected_plan': _final_selected_plan_evidence_value(context.get('selected_plan')),
        'deployment': _final_target_evidence_value(context.get('deployment')),
    }, ensure_ascii=False)
    for source, text in (('step', step), ('handoff', handoff),
                         ('intent', _step_evidence(response, 'intent_parsing')),
                         ('architecture', _step_evidence(response, 'architecture_planning'))):
        for target, markers in (('security_group', SECURITY_GROUP_MARKERS), ('vswitch', VSWITCH_MARKERS)):
            diagnostics['final_target_' + source + '_' + target] = _has_any_marker(text, markers)


def _check_final_target_resource_types(h: Any) -> None:
    """Check resource types, never a description saying a resource is forbidden."""
    facts = h.diagnostics
    if getattr(getattr(h, 'args', None), 'allow_real_cloud', False):
        inspected = facts.get('final_target_native_resources_inspected') is True
        security_group = facts.get('final_target_native_security_group_count', 0) > 0
        vswitch = facts.get('final_target_native_vswitch_count', 0) > 0
    else:
        # Offline fixtures must supply an actual template Resources object.
        inspected = facts.get('final_target_template_resources_inspected') is True
        security_group = facts.get('final_target_template_security_group') is True
        vswitch = facts.get('final_target_template_vswitch') is True
    h.checks['final deploying target resources inspected'] = inspected
    h.checks['final deploying target is security group'] = inspected and security_group
    h.checks['final deploying target is not VSwitch'] = inspected and not vswitch


def _native_final_target_resource_facts(h: Any, response: Any) -> dict[str, Any]:
    """Read only the final Stack identified by this session's accepted creation ledger."""
    facts: dict[str, Any] = {'final_target_native_resources_inspected': False}
    stack_id = _snapshot_current_stack_id(response, exclude=set()) or _latest_observed_stack_id(h, exclude=set())
    receipt = next((item for item in _cleanup_ledger_items(h, 'observed_resources')
                    if _is_ros_stack_resource(item)
                    and str(item.get('observed_action') or item.get('action') or '') == 'CreateStack'
                    and _string_from_mapping(item, 'resource_id', 'resourceId', 'stack_id', 'stackId') == stack_id),
                   None)
    if not stack_id or receipt is None:
        facts['final_target_native_probe_category'] = 'receipt_unavailable'
        return facts
    region = _string_from_mapping(receipt, 'region_id', 'regionId', 'RegionId')
    if not region:
        facts['final_target_native_probe_category'] = 'region_unavailable'
        return facts
    try:
        from alibabacloud_ros20190910 import models
        from alibabacloud_tea_util.models import RuntimeOptions

        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        credential = CloudCredentials().get_provider('aliyun')
        client = RosClientFactory.create(credential, region)
        options = RuntimeOptions(connect_timeout=5000, read_timeout=10000, autoretry=False)
        stack = client.get_stack_with_options(models.GetStackRequest(stack_id=stack_id, region_id=region), options)
        if stack.body.to_map().get('Status') != 'CREATE_COMPLETE':
            facts['final_target_native_probe_category'] = 'stack_not_complete'
            return facts
        result = client.list_stack_resources_with_options(
            models.ListStackResourcesRequest(stack_id=stack_id, region_id=region), options)
        resources = result.body.to_map().get('Resources')
        if (not isinstance(resources, list) or len(resources) > 1000
            or any(not isinstance(item, dict) or not isinstance(item.get('ResourceType'), str) for item in resources)):
            facts['final_target_native_probe_category'] = 'invalid_response'
            return facts
        facts.update(final_target_native_resources_inspected=True, final_target_native_probe_category='succeeded',
                     final_target_native_resource_count=len(resources),
                     final_target_native_security_group_count=sum(
                         item['ResourceType'] == 'ALIYUN::ECS::SecurityGroup' for item in resources),
                     final_target_native_vswitch_count=sum(
                         item['ResourceType'] == 'ALIYUN::ECS::VSwitch' for item in resources))
    except Exception:
        # No raw SDK error, identity or cloud response leaves the worker.
        facts['final_target_native_probe_category'] = 'query_failed'
    return facts


def _final_deployment_evidence(response: Any) -> str:
    evidence: dict[str, Any] = {"deploying_step": _step_evidence(response, "deploying")}
    handoff_context = _handoff_context(response)
    if isinstance(handoff_context, dict):
        selected_plan = _final_selected_plan_evidence_value(handoff_context.get("selected_plan"))
        deployment = _final_target_evidence_value(handoff_context.get("deployment"))
        handoff_target = {"selected_plan": selected_plan, "deployment": deployment}
        if not selected_plan:
            handoff_target["evaluated_candidates"] = _final_target_evidence_value(
                handoff_context.get("evaluated_candidates")
            )
        evidence["handoff_target"] = handoff_target
    return json.dumps(evidence, ensure_ascii=False, default=str)


def _final_selected_plan_evidence_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return _final_target_evidence_value(value)

    realized: dict[str, Any] = {}
    for key, nested in value.items():
        if key not in FINAL_SELECTED_PLAN_REALIZED_KEYS:
            continue
        if key == "selected_candidate_result":
            projected_value = _final_candidate_result_evidence_value(nested)
        else:
            projected_value = _final_target_evidence_value(nested)
        if projected_value not in (None, {}, []):
            realized[key] = projected_value
    if realized:
        return realized
    return _final_target_evidence_value(value)


def _final_candidate_result_evidence_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return _final_target_evidence_value(value)

    realized: dict[str, Any] = {}
    for key, nested in value.items():
        if key not in FINAL_CANDIDATE_RESULT_EVIDENCE_KEYS:
            continue
        projected_value = _final_target_evidence_value(nested)
        if projected_value not in (None, {}, []):
            realized[key] = projected_value
    return realized


def _final_target_evidence_value(value: Any) -> Any:
    if isinstance(value, dict):
        action = str(value.get("action") or "").strip().lower().replace("-", "_")
        if action in FINAL_TARGET_EXCLUDED_ACTIONS:
            return {}
        projected: dict[str, Any] = {}
        for key, nested in value.items():
            if key not in FINAL_TARGET_EVIDENCE_KEYS:
                continue
            if key == "template" and isinstance(nested, str):
                continue
            projected_value = _final_target_evidence_value(nested)
            if projected_value not in (None, {}, []):
                projected[key] = projected_value
        return projected
    if isinstance(value, list):
        projected_items = [_final_target_evidence_value(item) for item in value]
        return [item for item in projected_items if item not in (None, {}, [])]
    return value


def _handoff_context(response: Any) -> dict[str, Any] | None:
    snapshot = _snapshot(response)
    handoff = snapshot.get("normalHandoff") if isinstance(snapshot, dict) else None
    if not isinstance(handoff, dict):
        return None
    summary = handoff.get("summary")
    data = handoff.get("data")
    if not isinstance(summary, str) and isinstance(data, dict):
        summary = data.get("summary")
    if not isinstance(summary, str):
        return None
    marker = "Included context:\n"
    start = summary.find(marker)
    if start < 0:
        return None
    start += len(marker)
    try:
        # Product handoffs append missing-field and safety sections after the
        # included JSON. Read that one object, rather than treating the prose
        # between it and the final usage instruction as part of the JSON.
        value, _ = json.JSONDecoder().raw_decode(summary[start:].lstrip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _a2a_session_contains_user_message(h: Any, text: str) -> bool:
    context_id = str(getattr(h, "context_id", "") or "")
    if not context_id:
        _append_harness_note(h, "cannot inspect A2A session: context id is unknown")
        return False
    context_path = Path(getattr(h, "run_dir", "")) / "a2a-persistence" / "contexts" / f"{context_id}.json"
    try:
        context_record = json.loads(context_path.read_text(encoding="utf-8"))
    except OSError as exc:
        _append_harness_note(h, f"cannot inspect A2A session: {type(exc).__name__} reading {context_path}")
        return False
    except json.JSONDecodeError:
        _append_harness_note(h, f"cannot inspect A2A session: invalid JSON in {context_path}")
        return False
    if not isinstance(context_record, dict):
        _append_harness_note(h, f"cannot inspect A2A session: invalid context record in {context_path}")
        return False

    session_id = str(context_record.get("session_id") or "")
    cwd = str(context_record.get("cwd") or getattr(h, "cwd", "") or "")
    if not session_id or not cwd:
        _append_harness_note(h, f"cannot inspect A2A session: missing cwd/session_id in {context_path}")
        return False
    try:
        messages = SessionStorage().load(cwd, session_id)
    except Exception as exc:
        _append_harness_note(h, f"cannot inspect A2A session: {type(exc).__name__} loading {session_id}")
        return False
    return any(
        getattr(message, "role", "") == "user" and text in _agent_message_text(message)
        for message in messages
    )


def _agent_message_text(message: Any) -> str:
    to_dict = getattr(message, "to_dict", None)
    data = to_dict() if callable(to_dict) else {}
    content = data.get("content") if isinstance(data, dict) else None
    return _message_content_text(content)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _append_harness_note(h: Any, note: str) -> None:
    notes = getattr(h, "notes", None)
    if isinstance(notes, list):
        notes.append(note)


def _pending_kind(response: Any) -> str:
    snapshot = _snapshot(response)
    pending = snapshot.get("pendingInput") if isinstance(snapshot, dict) else None
    return str(pending.get("kind") or "") if isinstance(pending, dict) else ""


def _pending_step_id(response: Any) -> str:
    snapshot = _snapshot(response)
    pending = snapshot.get("pendingInput") if isinstance(snapshot, dict) else None
    step = pending.get("step") if isinstance(pending, dict) else None
    return str(step.get("id") or "") if isinstance(step, dict) else ""


def _latest_pending_kind(path: Path) -> str:
    kind = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return kind
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for envelope in _extract_pipeline_envelopes(value):
            if envelope.get("eventType") != "input_required":
                continue
            data = envelope.get("data")
            if isinstance(data, dict):
                kind = str(data.get("kind") or "")
    return kind


def _latest_input_required_kind_from_events(events: Iterable[Any]) -> str:
    kind = ""
    for value in events:
        for envelope in _extract_pipeline_envelopes(value):
            if envelope.get("eventType") != "input_required":
                continue
            data = envelope.get("data")
            if isinstance(data, dict):
                kind = str(data.get("kind") or "")
    return kind


def _latest_input_required_step_id_from_events(events: Iterable[Any]) -> str:
    step_id = ""
    for value in events:
        for envelope in _extract_pipeline_envelopes(value):
            if envelope.get("eventType") != "input_required":
                continue
            step = envelope.get("step")
            if isinstance(step, dict) and step.get("id"):
                step_id = str(step.get("id") or "")
            data = envelope.get("data")
            if isinstance(data, dict) and data.get("stepId"):
                step_id = str(data.get("stepId") or "")
    return step_id


def _deployment_succeeded_with_stack_id(run_dir: Path) -> bool:
    latest_rank: tuple[float, int] | None = None
    latest_conclusion: dict[str, Any] = {}
    event_order = 0
    for path in sorted(run_dir.glob("*.events.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            for envelope in _extract_pipeline_envelopes(value):
                event_order += 1
                step = envelope.get("step")
                if envelope.get("eventType") != "step_completed" or not isinstance(step, dict):
                    continue
                if step.get("id") != "deploying":
                    continue
                data = envelope.get("data")
                conclusion = data.get("conclusion") if isinstance(data, dict) else None
                if not isinstance(conclusion, dict):
                    continue
                try:
                    sequence = float(envelope.get("sequence"))
                except (TypeError, ValueError):
                    sequence = float("-inf")
                rank = (sequence, event_order)
                if latest_rank is None or rank > latest_rank:
                    latest_rank = rank
                    latest_conclusion = conclusion

    if str(latest_conclusion.get("status") or "").casefold() != "success":
        return False
    stack_id = latest_conclusion.get("stack_id") or latest_conclusion.get("stackId")
    outputs = latest_conclusion.get("outputs")
    if not stack_id and isinstance(outputs, dict):
        stack_id = outputs.get("StackId") or outputs.get("stackId") or outputs.get("stack_id")
    return isinstance(stack_id, str) and bool(stack_id.strip())


def _all_evidence(h: ScenarioHarness) -> str:
    return json.dumps(
        {
            "summaries": {name: asdict(summary) for name, summary in h.summaries.items()},
            "snapshots": h.snapshots,
            "workspace": _workspace_text_evidence(h),
        },
        ensure_ascii=False,
        default=str,
    )


def _workspace_text_evidence(h: ScenarioHarness) -> dict[str, str]:
    workspace_dir = getattr(h, "workspace_dir", None)
    if workspace_dir is None:
        return {}
    root = Path(workspace_dir)
    if not root.is_dir():
        return {}

    evidence: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in EVIDENCE_TEXT_FILE_SUFFIXES:
            continue
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            if path.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
                evidence[relative_path] = "<skipped: file too large>"
                continue
            evidence[relative_path] = path.read_text(encoding="utf-8")
        except OSError as exc:
            evidence[relative_path] = f"<skipped: {type(exc).__name__}>"
        except UnicodeDecodeError:
            evidence[relative_path] = "<skipped: non-utf8 text file>"
    return evidence


def _has_any_marker(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _join_stream_or_note(stream: BackgroundStream, h: ScenarioHarness) -> None:
    try:
        stream.join(timeout=h.args.stream_timeout)
    except Exception as exc:
        h.notes.append(f"{stream.name} ended while joining: {type(exc).__name__}: {exc}")


def _post_rollback_timeout(args: argparse.Namespace) -> float:
    event_timeout = float(getattr(args, "event_timeout", 0) or 0)
    stream_timeout = float(getattr(args, "stream_timeout", 0) or 0)
    return max(event_timeout, min(stream_timeout, 900.0))


def _cleanup_deployment_prompt(base_prompt: str, h: ScenarioHarness, label: str) -> str:
    completion_instruction = (
        "本轮是回滚窗口验证：CreateStack 成功后不要调用 complete_step，不要结束 deploying step；"
        "只简短说明新建的 stack_id，并等待用户下一条指令。"
        if label == "first"
        else "complete_step 前必须在本轮对话中看到一次新的 CreateStack 成功，部署总结的 stack_id 必须来自这次新建。"
    )
    return (
        f"{base_prompt}\n\n"
        "E2E 强制部署约束：\n"
        "- 本轮必须新建一个 ROS stack，不能把已有 stack 的查询或等待作为部署成功依据。\n"
        f"{completion_instruction}"
        "创建 VSwitch 时请先检查目标 VPC 已有 VSwitch CIDR，选择未占用且属于 VPC CIDR 的网段；"
        "如果 CIDR 冲突，请选择另一个未占用网段。"
    )


def _cleanup_stack_name(h: ScenarioHarness, label: str) -> str:
    suffix = getattr(h, "run_id", "") or Path(getattr(h, "run_dir", "")).name.rsplit("-", maxsplit=1)[-1]
    safe_label = "".join(ch if ch.isalnum() else "-" for ch in label.lower()).strip("-") or "stack"
    return f"iac-e2e-{suffix[:12]}-{safe_label}"[:128]


def _wait_for_observed_cleanup_stack(
    h: ScenarioHarness,
    *,
    exclude: set[str],
    timeout: float,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stack_id = _latest_observed_stack_id(h, exclude=exclude)
        if stack_id:
            return stack_id
        time.sleep(1.0)
    raise TimeoutError("Timed out waiting for rollback cleanup ledger to observe a ROS stack")


def _wait_for_created_stack(
    stream: BackgroundStream,
    *,
    exclude: set[str],
    timeout: float,
    expected_stack_name: str | None = None,
) -> str:
    match = _wait_any(
        [stream],
        _created_stack_event(exclude, expected_stack_name=expected_stack_name),
        description="successful stack creation event",
        timeout=timeout,
    )
    stack_id = _created_stack_id_from_event(match.event, exclude=exclude, expected_stack_name=expected_stack_name)
    if not stack_id:
        raise RuntimeError("successful CreateStack event did not include a stack id")
    return stack_id


def _created_stack_ids_in_turn_files(run_dir: Path, names: set[str], *, exclude: set[str]) -> list[str]:
    """Include native receipts from parameter replies after the selected turn."""
    ids: list[str] = []
    for name in sorted(names):
        path = run_dir / f"{name}.events.jsonl"
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 20_000_000:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            stack_id = _created_stack_id_from_event(event, exclude=exclude)
            if stack_id:
                ids.append(stack_id)
    return _unique_strings(ids)


def _created_stack_id_from_stream(
    stream: Any,
    *,
    exclude: set[str],
    expected_stack_name: str | None = None,
) -> str | None:
    for event in getattr(stream, "events", []) or []:
        stack_id = _created_stack_id_from_event(event, exclude=exclude, expected_stack_name=expected_stack_name)
        if stack_id:
            return stack_id
    return None


def _created_stack_event(
    exclude: set[str],
    *,
    expected_stack_name: str | None = None,
) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        return _created_stack_id_from_event(
            event,
            exclude=exclude,
            expected_stack_name=expected_stack_name,
        ) is not None

    return predicate


def _created_stack_id_from_event(
    event: Any,
    *,
    exclude: set[str],
    expected_stack_name: str | None = None,
) -> str | None:
    for envelope in _extract_pipeline_envelopes(event):
        stack_id = _created_stack_id_from_envelope(
            envelope,
            exclude=exclude,
            expected_stack_name=expected_stack_name,
        )
        if stack_id:
            return stack_id
    return None


def _created_stack_id_from_envelope(
    envelope: dict[str, Any],
    *,
    exclude: set[str],
    expected_stack_name: str | None = None,
) -> str | None:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    event_type = envelope.get("eventType")
    if event_type == "stack_current_changed":
        if str(data.get("provider") or "").lower() != "ros":
            return None
        if data.get("action") not in STACK_CREATION_SUCCESS_ACTIONS or data.get("isSuccess") is not True:
            return None
        if not _stack_name_matches(data, expected_stack_name):
            return None
        stack_id = _string_from_mapping(data, "stackId", "stack_id", "StackId")
        return stack_id if stack_id and stack_id not in exclude else None
    if event_type == "tool_result":
        return _created_stack_id_from_ros_deploy_tool_result(
            data,
            exclude=exclude,
            expected_stack_name=expected_stack_name,
        )
    return None


def _created_stack_id_from_ros_deploy_tool_result(
    data: dict[str, Any],
    *,
    exclude: set[str],
    expected_stack_name: str | None = None,
) -> str | None:
    if str(data.get("toolName") or data.get("tool_name") or "") != "ros_deploy":
        return None
    if data.get("isError") is True or data.get("is_error") is True:
        return None
    result = data.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    if not isinstance(result, dict):
        return None
    if result.get("is_success") is not True:
        return None
    status = str(result.get("status") or result.get("stack_status") or "")
    if status and status != "CREATE_COMPLETE":
        return None
    if not _stack_name_matches(result, expected_stack_name):
        return None
    stack_id = _string_from_mapping(result, "stackId", "stack_id", "StackId")
    return stack_id if stack_id and stack_id not in exclude else None


def _stack_name_matches(mapping: dict[str, Any], expected_stack_name: str | None) -> bool:
    if not expected_stack_name:
        return True
    stack_name = _string_from_mapping(mapping, "stackName", "stack_name", "StackName", "name")
    return stack_name == expected_stack_name


def _latest_observed_stack_id(h: ScenarioHarness, *, exclude: set[str]) -> str | None:
    resources = _cleanup_ledger_items(h, "observed_resources")
    for resource in reversed(resources):
        if not _is_ros_stack_resource(resource):
            continue
        if str(resource.get("observed_action") or resource.get("action") or "") != "CreateStack":
            continue
        stack_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if stack_id and stack_id not in exclude:
            return stack_id
    return None


def _cleanup_ledger_items(h: ScenarioHarness, key: str) -> list[dict[str, Any]]:
    if not getattr(h, "context_id", ""):
        return []
    try:
        from iac_code.services.session_storage import SessionStorage

        cwd, session_id = _pipeline_session_identity(h)
        config_dir = getattr(h, "server_env", {}).get("IAC_CODE_CONFIG_DIR")
        storage = SessionStorage(projects_dir=Path(config_dir) / "projects") if config_dir else SessionStorage()
        session_dir = storage.session_dir(cwd, session_id)
        paths = [session_dir / "pipeline" / "cleanup.yaml", session_dir / "a2a" / "pipeline" / "cleanup.yaml"]
        data = None
        for path in paths:
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                break
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    values = data.get(key)
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _pipeline_session_identity(h: ScenarioHarness) -> tuple[str, str]:
    context_id = str(getattr(h, "context_id", "") or "")
    cwd = str(getattr(h, "cwd", "") or "")
    run_dir_value = getattr(h, "run_dir", None)
    if context_id and run_dir_value is not None:
        path = Path(run_dir_value) / "a2a-persistence" / "contexts" / f"{context_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            session_id = data.get("session_id")
            persisted_cwd = data.get("cwd")
            if isinstance(session_id, str) and session_id:
                return (persisted_cwd if isinstance(persisted_cwd, str) and persisted_cwd else cwd, session_id)
    return cwd, context_id


def _wait_for_cleanup_started(
    h: ScenarioHarness,
    stream: BackgroundStream,
    stack_id: str,
    *,
    timeout: float,
) -> None:
    try:
        _wait_any(
            [stream],
            _cleanup_event_for_stack(stack_id, {"cleanup_started", "cleanup_progress"}),
            description=f"cleanup_started({stack_id})",
            timeout=timeout,
        )
        return
    except Exception as exc:
        h.notes.append(f"did not observe cleanup_started event before fallback: {exc}")
    _wait_for_cleanup_ledger_status(h, stack_id, {"started", "in_progress"}, timeout=timeout)


def _wait_for_cleanup_ledger_status(
    h: ScenarioHarness,
    stack_id: str,
    statuses: set[str],
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for resource in _cleanup_ledger_items(h, "cleanup_resources"):
            if _string_from_mapping(resource, "resource_id", "resourceId") != stack_id:
                continue
            if str(resource.get("cleanup_status") or resource.get("cleanupStatus") or "") in statuses:
                return
        time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for cleanup ledger status {sorted(statuses)} on {stack_id}")


def _cleanup_event_for_stack(
    stack_id: str,
    event_types: set[str],
) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        return any(
            envelope.get("eventType") in event_types
            and isinstance(envelope.get("data"), dict)
            and envelope["data"].get("resourceId") == stack_id
            for envelope in _extract_pipeline_envelopes(event)
        )

    return predicate


def _events_file_has_cleanup_event(path: Path, *, stack_id: str, event_types: set[str]) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for envelope in _extract_pipeline_envelopes(value):
            if envelope.get("eventType") not in event_types:
                continue
            data = envelope.get("data")
            if isinstance(data, dict) and data.get("resourceId") == stack_id:
                return True
    return False


def _run_dir_has_cleanup_events(run_dir: Path) -> bool:
    return any(_events_file_has_cleanup_activity(path) for path in sorted(run_dir.glob("*.events.jsonl")))


def _events_file_has_cleanup_activity(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if any(_pipeline_envelope_has_cleanup_activity(envelope) for envelope in _extract_pipeline_envelopes(value)):
            return True
    return False


def _pipeline_envelope_has_cleanup_activity(envelope: dict[str, Any]) -> bool:
    if envelope.get("eventType") in CLEANUP_EVENT_TYPES or envelope.get("scope") == "cleanup":
        return True
    data = envelope.get("data")
    cleanup = data.get("cleanup") if isinstance(data, dict) else None
    return isinstance(cleanup, dict) and _cleanup_payload_has_targets(cleanup)


def _cleanup_resource_for_stack(response: Any, stack_id: str | None) -> dict[str, Any] | None:
    if not stack_id:
        return None
    cleanup = _snapshot_cleanup(response)
    resources = cleanup.get("resources") if isinstance(cleanup, dict) else None
    if not isinstance(resources, list):
        return None
    for resource in resources:
        if isinstance(resource, dict) and resource.get("resourceId") == stack_id:
            return resource
    return None


def _cleanup_target_stack_ids(h: ScenarioHarness, *, exclude: set[str]) -> list[str]:
    stack_ids: list[str] = []
    for resource in _cleanup_ledger_items(h, "cleanup_resources"):
        if not _is_ros_stack_resource(resource):
            continue
        if resource.get("cleanup_required") is False or resource.get("cleanupRequired") is False:
            continue
        stack_id = _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId")
        if stack_id and stack_id not in exclude:
            stack_ids.append(stack_id)
    return _unique_strings(stack_ids)


def _cleanup_resource_completed(resource: dict[str, Any] | None) -> bool:
    if not isinstance(resource, dict):
        return False
    cleanup_status = resource.get("cleanupStatus") or resource.get("cleanup_status") or resource.get("status")
    stack_status = resource.get("stackStatus") or resource.get("progressStatus") or resource.get("progress_status")
    return cleanup_status == "completed" and stack_status == "DELETE_COMPLETE"


def _rollback_cleanup_diagnostics(
    h: ScenarioHarness,
    cleanup_summary: StreamSummary,
    first_stack_id: str | None,
    cleanup_stack_ids: list[str],
    after_cleanup: Any,
    ros_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    resources = _cleanup_ledger_items(h, "cleanup_resources")
    tool_uses = _cleanup_ledger_items(h, "tool_uses")
    history = _cleanup_ledger_items(h, "history")
    first_ledger = next(
        (
            item for item in resources if _string_from_mapping(item, "resource_id", "resourceId") == first_stack_id
        ),
        None,
    )
    first_snapshot = _cleanup_resource_for_stack(after_cleanup, first_stack_id)
    first_ros = ros_states.get(first_stack_id, {}) if first_stack_id else {}
    first_region = first_ledger.get("region_id") if first_ledger else None
    failures = [
        item for item in history
        if item.get("type") == "cleanup_failed"
        and isinstance(item.get("resource"), dict)
        and item["resource"].get("resource_id") == first_stack_id
    ]
    failure_by_action = {
        action: next((item for item in reversed(failures) if item.get("cleanup_action") == action), None)
        for action in ("DeleteStack", "GetStack")
    }
    allowed_cleanup_statuses = {"pending", "started", "in_progress", "completed", "failed", "unknown"}
    allowed_ros_statuses = {
        "CREATE_COMPLETE", "DELETE_STARTED", "DELETE_IN_PROGRESS", "DELETE_COMPLETE", "DELETE_FAILED",
    }

    def status(value: Any, allowed: set[str]) -> str:
        return value if isinstance(value, str) and value in allowed else "unknown"

    try:
        from iac_code.pipeline.engine.cleanup import is_active_cleanup_prompt_message

        cwd, session_id = _pipeline_session_identity(h)
        active_prompt = any(
            is_active_cleanup_prompt_message(message) for message in SessionStorage().load(cwd, session_id)
        )
    except Exception:
        active_prompt = False
    diagnostics = {
        "cleanup_turn_event_count": cleanup_summary.event_count,
        "cleanup_turn_cleanup_event_count": sum(
            event_type in {"cleanup_started", "cleanup_progress", "cleanup_completed", "cleanup_failed"}
            for event_type in cleanup_summary.pipeline_event_types
        ),
        "cleanup_target_count": len(cleanup_stack_ids),
        "cleanup_ledger_pending_count": sum(
            item.get("cleanup_status") != "completed" for item in resources if item.get("cleanup_required") is not False
        ),
        "cleanup_delete_tool_use_count": sum(item.get("action") == "DeleteStack" for item in tool_uses),
        "cleanup_get_tool_use_count": sum(item.get("action") == "GetStack" for item in tool_uses),
        "cleanup_delete_tool_kind": _cleanup_tool_kind(tool_uses, "DeleteStack"),
        "cleanup_get_tool_kind": _cleanup_tool_kind(tool_uses, "GetStack"),
        "cleanup_delete_target_matches": all(
            item.get("resource_id") == first_stack_id and item.get("region_id") == first_region
            for item in tool_uses if item.get("action") == "DeleteStack"
        ),
        "cleanup_get_target_matches": all(
            item.get("resource_id") == first_stack_id and item.get("region_id") == first_region
            for item in tool_uses if item.get("action") == "GetStack"
        ),
        "cleanup_failure_event_count": len(failures),
        "cleanup_prompt_active": active_prompt,
        "cleanup_turn_terminal_state": status(
            cleanup_summary.last_status_state,
            {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"},
        ),
        "cleanup_first_ledger_status": status(
            first_ledger.get("cleanup_status") if first_ledger else None, allowed_cleanup_statuses
        ),
        "cleanup_first_snapshot_status": status(
            first_snapshot.get("cleanupStatus") if first_snapshot else None, allowed_cleanup_statuses
        ),
        "cleanup_first_ros_status": status(first_ros.get("status"), allowed_ros_statuses),
        "cleanup_first_ros_not_found": first_ros.get("not_found") is True,
    }
    for action, prefix in (("DeleteStack", "delete"), ("GetStack", "get")):
        failure = failure_by_action[action]
        if failure is None:
            continue
        code, http_status = _cleanup_failure_code_and_http_status(failure.get("last_error"))
        if code:
            diagnostics[f"cleanup_{prefix}_error_code"] = code
        if http_status:
            diagnostics[f"cleanup_{prefix}_http_status"] = http_status
        diagnostics[f"cleanup_{prefix}_error_kind"] = _cleanup_failure_kind(failure.get("last_error"))
    return diagnostics


def _cleanup_tool_kind(tool_uses: list[dict[str, Any]], action: str) -> str:
    names = {str(item.get("tool_name") or "") for item in tool_uses if item.get("action") == action}
    return next(iter(names)) if len(names) == 1 and names <= {"aliyun_api", "ros_stack"} else "unknown"


def _cleanup_failure_code_and_http_status(value: Any) -> tuple[str, int | None]:
    if not isinstance(value, str):
        return "", None
    code_match = re.search(
        r"(?:error code|[\"']?[Cc]ode[\"']?\s*[:=])\s*[\"']?([A-Za-z][A-Za-z0-9_.-]{0,79})",
        value,
        re.IGNORECASE,
    )
    status_match = re.search(r"\bHTTP\s+([45][0-9]{2})\b", value)
    return (
        code_match.group(1).rstrip(".") if code_match else "",
        int(status_match.group(1)) if status_match else None,
    )


def _cleanup_failure_kind(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    lowered = value.casefold()
    for kind, markers in (
        ("permission", ("forbidden", "permission", "accessdenied", "denied")),
        ("credential", ("credential", "authenticate", "signature")),
        ("not_found", ("notfound", "not found", "nonexistent")),
        ("resource_busy", ("inoperation", "operationinprogress", "busy", "in use")),
        ("rate_limited", ("throttl", "ratelimit")),
        ("invalid_input", ("invalidparameter", "invalid parameter", "invalidinput")),
        ("timeout", ("timeout", "timed out")),
        ("network", ("connection", "network", "endpoint")),
    ):
        if any(marker in lowered for marker in markers):
            return kind
    return "unknown"


def _snapshot_cleanup(response: Any) -> dict[str, Any]:
    snapshot = _snapshot(response)
    cleanup = snapshot.get("cleanup") if isinstance(snapshot, dict) else None
    return cleanup if isinstance(cleanup, dict) else {}


def _snapshot_has_cleanup_activity(response: Any) -> bool:
    return _cleanup_payload_has_targets(_snapshot_cleanup(response))


def _cleanup_payload_has_targets(cleanup: dict[str, Any]) -> bool:
    resources = cleanup.get("resources")
    if isinstance(resources, list) and any(_cleanup_resource_has_target(item) for item in resources):
        return True
    history = cleanup.get("history")
    if isinstance(history, list) and any(_cleanup_history_item_has_activity(item) for item in history):
        return True
    resource_count = cleanup.get("resourceCount", cleanup.get("resource_count"))
    if _positive_int(resource_count):
        return True
    status = str(cleanup.get("status") or "")
    return status in CLEANUP_ACTIVE_STATUSES


def _cleanup_resource_has_target(resource: Any) -> bool:
    if not isinstance(resource, dict):
        return False
    target_keys = (
        "resourceId",
        "resource_id",
        "stackId",
        "stack_id",
        "physicalResourceId",
        "physical_resource_id",
    )
    return any(str(resource.get(key) or "").strip() for key in target_keys)


def _cleanup_history_item_has_activity(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    event_type = str(item.get("eventType") or item.get("event_type") or "")
    if event_type in CLEANUP_EVENT_TYPES:
        return True
    if str(item.get("status") or "") in CLEANUP_ACTIVE_STATUSES:
        return True
    resource_count = item.get("resourceCount", item.get("resource_count"))
    if _positive_int(resource_count):
        return True
    resources = item.get("resources")
    if isinstance(resources, list) and any(_cleanup_resource_has_target(resource) for resource in resources):
        return True
    data = item.get("data")
    if isinstance(data, dict) and (_cleanup_resource_has_target(data) or _cleanup_payload_has_targets(data)):
        return True
    return False


def _positive_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        try:
            return int(value) > 0
        except ValueError:
            return False
    return False


def _cleanup_ledger_has_required_resources(h: ScenarioHarness) -> bool:
    for resource in _cleanup_ledger_items(h, "cleanup_resources"):
        if resource.get("cleanup_required") is False or resource.get("cleanupRequired") is False:
            continue
        return True
    return False


def _session_has_cleanup_prompt(h: ScenarioHarness) -> bool:
    if not getattr(h, "context_id", ""):
        return False
    try:
        from iac_code.services.session_storage import SessionStorage

        cwd, session_id = _pipeline_session_identity(h)
        return _session_file_has_cleanup_prompt(SessionStorage().session_path(cwd, session_id))
    except OSError:
        return False


def _session_file_has_cleanup_prompt(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        metadata = value.get("metadata") if isinstance(value, dict) else None
        if isinstance(metadata, dict) and metadata.get("type") == CLEANUP_PROMPT_METADATA_TYPE:
            return True
    return False


def _snapshot_current_stack_id(response: Any, *, exclude: set[str]) -> str | None:
    snapshot = _snapshot(response)
    stacks = snapshot.get("stacks") if isinstance(snapshot, dict) else None
    if not isinstance(stacks, dict):
        return None
    current = stacks.get("current")
    current_id = _active_stack_id_from_record(current)
    if current_id and current_id not in exclude:
        return current_id
    by_id = stacks.get("byId")
    if isinstance(by_id, dict):
        for record in reversed(list(by_id.values())):
            stack_id = _active_stack_id_from_record(record)
            if stack_id and stack_id not in exclude:
                return stack_id
    history = stacks.get("history")
    if isinstance(history, list):
        for record in reversed(history):
            stack_id = _active_stack_id_from_record(record)
            if stack_id and stack_id not in exclude:
                return stack_id
    return None


def _active_stack_id_from_record(record: Any) -> str | None:
    if not isinstance(record, dict):
        return None
    if record.get("current") is False or record.get("cleared") is True:
        return None
    if record.get("isSuccess") is False:
        return None
    status = str(record.get("stackStatus") or record.get("status") or "")
    if status.endswith("_FAILED"):
        return None
    action = record.get("action")
    if action == "DeleteStack":
        return None
    return _string_from_mapping(record, "stackId", "stack_id", "StackId", "id")


def _capture_ros_stack_states(h: ScenarioHarness, stack_ids: Iterable[str], name: str) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for stack_id in stack_ids:
        region_id = _region_for_stack(h, stack_id)
        states[stack_id] = _get_ros_stack_state(stack_id=stack_id, region_id=region_id, redaction_env=h.server_env)
    redacted = _redact_json_value(states, h.server_env)
    _write_json(h.run_dir / f"{name}.ros-stack-states.json", redacted)
    h.snapshots[f"{name}.ros-stack-states"] = redacted
    return states


def _get_ros_stack_state(
    *,
    stack_id: str,
    region_id: str,
    redaction_env: dict[str, str] | None,
) -> dict[str, Any]:
    try:
        from alibabacloud_ros20190910 import models as ros_models

        from iac_code.services.cloud_credentials import CloudCredentials
        from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory

        credential = CloudCredentials().get_provider("aliyun")
        effective_region = region_id or (credential.region_id if credential is not None else "")
        client = RosClientFactory.create(credential, effective_region)
        request = ros_models.GetStackRequest(stack_id=stack_id, region_id=effective_region)
        response = client.get_stack(request)
        body = response.body.to_map()
        return {
            "stack_id": str(body.get("StackId") or stack_id),
            "stack_name": str(body.get("StackName") or ""),
            "region_id": effective_region,
            "status": str(body.get("Status") or ""),
            "status_reason": str(body.get("StatusReason") or ""),
            "not_found": False,
        }
    except Exception as exc:
        message = _redact_sensitive_text(str(exc), redaction_env)
        return {
            "stack_id": stack_id,
            "region_id": region_id,
            "status": "",
            "not_found": _is_ros_stack_not_found(exc),
            "error": _compact_text(message, max_chars=1000),
        }


def _is_ros_stack_not_found(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "")
    message = str(exc)
    combined = f"{code} {message}".lower()
    not_found_tokens = (
        "stacknotfound",
        "notfound.stack",
        "entitynotexist.stack",
        "specified stack does not exist",
        "stack could not be found",
        "stack not found",
    )
    return any(token in combined for token in not_found_tokens)


def _region_for_stack(h: ScenarioHarness, stack_id: str) -> str:
    for snapshot in reversed(list(h.snapshots.values())):
        region = _region_for_stack_in_snapshot(snapshot, stack_id)
        if region:
            return region
    for key in ("cleanup_resources", "observed_resources"):
        for resource in reversed(_cleanup_ledger_items(h, key)):
            if _string_from_mapping(resource, "resource_id", "resourceId", "stack_id", "stackId") == stack_id:
                region = _string_from_mapping(resource, "region_id", "regionId", "RegionId")
                if region:
                    return region
    return h.server_env.get("ALIBABA_CLOUD_REGION_ID", "")


def _region_for_stack_in_snapshot(response: Any, stack_id: str) -> str:
    cleanup_resource = _cleanup_resource_for_stack(response, stack_id)
    if cleanup_resource is not None:
        region = _string_from_mapping(cleanup_resource, "regionId", "region_id", "RegionId")
        if region:
            return region
    snapshot = _snapshot(response)
    stacks = snapshot.get("stacks") if isinstance(snapshot, dict) else None
    if not isinstance(stacks, dict):
        return ""
    by_id = stacks.get("byId")
    if isinstance(by_id, dict):
        record = by_id.get(stack_id)
        region = _string_from_mapping(record, "regionId", "region_id", "RegionId") if isinstance(record, dict) else None
        if region:
            return region
    current = stacks.get("current")
    if isinstance(current, dict) and _string_from_mapping(current, "stackId", "stack_id", "StackId") == stack_id:
        return _string_from_mapping(current, "regionId", "region_id", "RegionId") or ""
    return ""


def _ros_stack_deleted(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("not_found") is True:
        return True
    return state.get("status") in ROS_STACK_DELETED_STATUSES


def _ros_stack_retained(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict) or state.get("not_found") is True:
        return False
    status = state.get("status")
    return isinstance(status, str) and bool(status) and not status.startswith("DELETE_")


def _is_ros_stack_resource(resource: dict[str, Any]) -> bool:
    provider = str(resource.get("provider") or "").lower()
    resource_type = str(resource.get("resource_type") or resource.get("resourceType") or "").lower()
    return provider == "ros" and resource_type == "stack"


def _unique_strings(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _string_from_mapping(mapping: Any, *keys: str) -> str | None:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _scenario_run_dir(args: argparse.Namespace, scenario: str) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser()
    root = Path(args.run_root).expanduser()
    return _new_run_dir(root / scenario)


def _print_result(result: ScenarioRunResult) -> None:
    print(f"\nA2A session recovery scenario: {result.scenario}")
    print(f"run_dir: {result.run_dir}")
    print(f"server_url: {result.server_url}")
    print(f"context_id: {result.context_id}")
    print(f"pipeline_task_id: {result.pipeline_task_id}")
    if result.abort_reason:
        print(f"abort_reason: {_compact_text(result.abort_reason, max_chars=1000)}")
    if result.notes:
        print("\nnotes:")
        for note in result.notes:
            print(f"  - {_compact_text(note, max_chars=1000)}")
    print("\nchecks:")
    for name, passed in result.checks.items():
        print(f"  {'OK' if passed else 'FAIL'} {name}")
    print(f"\nRESULT: {'PASS' if result.passed else 'FAIL'}")


def _validate_scenario_execution(args: argparse.Namespace, scenario: str) -> None:
    if scenario == "fault-after-snapshot" and not args.deterministic:
        raise SystemExit("scenario fault-after-snapshot requires --deterministic")
    if scenario in _REAL_CLOUD_SCENARIOS and not args.allow_real_cloud:
        raise SystemExit(
            "refusing to run provider/tool/cloud recovery scenario without --allow-real-cloud: " + scenario
        )


_RUNNING_STEP_SCENARIOS = {
    "step1-running": "intent_parsing",
    "step2-running": "architecture_planning",
    "step3-running": "evaluate_candidates",
    "step4-running": "confirm_and_select",
    "step5-running": "deploying",
}
_ROLLBACK_SCENARIOS = {
    "rollback-step1": "intent_parsing",
    "rollback-step2": "architecture_planning",
    "rollback-step3": "evaluate_candidates",
    "rollback-step4": "confirm_and_select",
    "rollback-step5": "deploying",
}
_CANCEL_SCENARIOS = {
    "cancel-step1": "intent_parsing",
    "cancel-step2": "architecture_planning",
    "cancel-step3": "evaluate_candidates",
    "cancel-step4": "confirm_and_select",
    "cancel-step5": "deploying",
}
_REAL_CLOUD_SCENARIOS = {
    "contract-graceful-cancel",
    "contract-graceful-success",
    "fault-after-snapshot",
    "image-ask-waiting",
    "image-initial",
    "image-interrupt",
    "image-normal-handoff",
    "image-selection-waiting",
    "scenario1",
    "scenario1-performance-backup",
    SELECTION_DURING_BACKUP_SCENARIO,
    "normal-running",
    "ask-waiting",
    REDACTION_STEP4_SCENARIO,
    IAC_CODE_WEB_2C4G_STEP4_SCENARIO,
    "selection-waiting",
    "rollback-step5-cleanup",
    "rollback-step5-cleanup-recovery",
    *_RUNNING_STEP_SCENARIOS,
    *_ROLLBACK_SCENARIOS,
    *_CANCEL_SCENARIOS,
}
_SCENARIOS: dict[str, Callable[[argparse.Namespace, str], int]] = {
    "contract-graceful-cancel": run_contract_graceful_cancel,
    "contract-graceful-success": run_contract_graceful_success,
    "image-ask-waiting": run_image_ask_waiting,
    "image-initial": run_image_initial,
    "image-interrupt": run_image_interrupt,
    "image-normal-handoff": run_image_normal_handoff,
    "image-selection-waiting": run_image_selection_waiting,
    "scenario1": run_scenario1,
    "scenario1-performance-backup": run_scenario1_performance_backup,
    SELECTION_DURING_BACKUP_SCENARIO: run_selection_during_backup,
    "normal-running": run_normal_running,
    "ask-waiting": run_ask_waiting,
    REDACTION_STEP4_SCENARIO: run_redaction_step4,
    IAC_CODE_WEB_2C4G_STEP4_SCENARIO: run_iac_code_web_2c4g_step4,
    "selection-waiting": run_selection_waiting,
    "fault-after-snapshot": run_fault_after_snapshot,
    "rollback-step5-cleanup": run_rollback_step5_cleanup,
    "rollback-step5-cleanup-recovery": run_rollback_step5_cleanup_recovery,
    **{name: run_running_step for name in _RUNNING_STEP_SCENARIOS},
    **{name: run_rollback for name in _ROLLBACK_SCENARIOS},
    **{name: run_cancel for name in _CANCEL_SCENARIOS},
}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--kill-server-pid":
        os.kill(int(sys.argv[2]), signal.SIGKILL)
        raise SystemExit(0)
    raise SystemExit(main())
