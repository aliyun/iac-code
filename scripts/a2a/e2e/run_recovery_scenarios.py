#!/usr/bin/env python3
"""Run A2A pipeline session-recovery E2E scenarios.

The scenarios in this file intentionally drive the public A2A JSON-RPC HTTP
endpoint. They do not call pipeline internals directly. Each run starts a local
A2A server, records raw requests/SSE/pipeline snapshots, kills the server with
SIGKILL at a scenario-specific point, restarts it with the same persistence
directory, then validates recovery behavior.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from PIL import Image, ImageDraw, ImageFont

E2E_SCRIPTS_DIR = Path(__file__).resolve().parent
A2A_SCRIPTS_DIR = E2E_SCRIPTS_DIR.parent
for scripts_dir in (E2E_SCRIPTS_DIR, A2A_SCRIPTS_DIR):
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
    _extract_pipeline_envelope,
    _parse_sse_data_line,
    build_message_stream_payload,
    build_task_cancel_payload,
    build_task_get_payload,
)

ASK_TRIGGER_PROMPT = "我有个产品要上线"
ASK_FIRST_ANSWER = "我要创建云网络资源；本次只选择已有 VPC 创建一个 VSwitch，不部署 ECS、EIP、SLB 或 Nginx。"
ASK_SECOND_ANSWER = "选择一个已有 VPC，创建一个 VSwitch；地域、可用区和网段你按低成本默认值推荐。"
INTERVENING_ASK_ANSWER = "使用默认配置（可用区和网段自动规划），继续。"
ROLLBACK_PROMPT = "回退到 intent_parsing，选择一个已有vpc，创建一个安全组"
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
IMAGE_TEXT_PROMPT = "请读取图片中的文字，并将图片中的文字作为本轮用户输入执行。"
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
SECURITY_GROUP_MARKERS = ("ALIYUN::ECS::SecurityGroup", "SecurityGroupId", "sg-", "安全组")
TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_INPUT_REQUIRED"}
ROS_STACK_DELETED_STATUSES = {"DELETE_COMPLETE"}


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

    def part(self, key: str, text: str) -> dict[str, Any]:
        safe_key = _safe_fixture_key(key)
        path = self._static_fixture_path(safe_key, text)
        source = "static"
        if path is None:
            path = self.root / f"{safe_key}.png"
            source = "generated"
            if not path.exists():
                path.write_bytes(_render_text_png(text))
        raw = path.read_bytes()
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


def _load_text_image_font(*, size: int) -> Any:
    candidates = [
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
        if Path(candidate).is_file():
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
        self._condition = threading.Condition()
        self._done = False
        self._thread = threading.Thread(target=self._run, name=f"a2a-e2e-{name}", daemon=True)

    @property
    def done(self) -> bool:
        with self._condition:
            return self._done

    def start(self) -> None:
        self._thread.start()

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
            model=args.model,
            api_base=args.api_base,
        )
        if args.deterministic:
            self.server_env["IAC_CODE_A2A_DETERMINISTIC_RECOVERY"] = "1"
            self.server_env["IAC_CODE_TEST_FAULT_INJECTION"] = "1"
            self.server_env["IAC_CODE_TEST_FAULT_INJECTION_MODE"] = "exit"
            if args.fault_at:
                self.server_env["IAC_CODE_TEST_CRASH_AT"] = args.fault_at
        self.server: ManagedServer | None = None
        self.server_index = 0
        self.context_id = ""
        self.pipeline_task_id = ""
        self.checks: dict[str, bool] = {}
        self.notes: list[str] = []
        self.summaries: dict[str, Any] = {}
        self.snapshots: dict[str, Any] = {}

    def preflight(self) -> None:
        if self.args.deterministic:
            self.checks["LLM preflight skipped for deterministic mode"] = True
            self.notes.append(
                "LLM preflight skipped because deterministic fault injection is enabled; "
                "post-restart execution still uses the configured pipeline/provider."
            )
            return
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
        wait_for_server(self.server_url, timeout=self.args.server_timeout)

    def kill9_and_restart(self) -> None:
        if self.server is None:
            raise RuntimeError("server is not running")
        self.server.kill9()
        self.start_server()

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
    ) -> StreamSummary:
        return self.stream(
            prompt=prompt,
            name=name,
            context_id=context_id,
            task_id=task_id,
            images=[self.image_fixtures.part(image_key, text)],
        )

    def start_stream(
        self,
        *,
        prompt: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> BackgroundStream:
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
        stream.wait_for(
            lambda _event, summary: bool(summary.context_id and summary.task_id),
            description="task identity",
            timeout=self.args.event_timeout,
        )
        self._remember_identity(stream.summary)
        self.summaries[name] = stream.summary
        return stream

    def start_stream_image_text(
        self,
        *,
        text: str,
        image_key: str,
        name: str,
        context_id: str | None = None,
        task_id: str | None = None,
        prompt: str = IMAGE_TEXT_PROMPT,
    ) -> BackgroundStream:
        return self.start_stream(
            prompt=prompt,
            name=name,
            context_id=context_id,
            task_id=task_id,
            images=[self.image_fixtures.part(image_key, text)],
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

    def finish(self, *, passed: bool | None = None, abort_reason: str = "") -> int:
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
            "streams": {name: asdict(summary) for name, summary in self.summaries.items()},
            "snapshots": self.snapshots,
        }
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
    parser.add_argument("--model", default="")
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
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--preflight-timeout", type=float, default=60.0)
    parser.add_argument("--server-timeout", type=float, default=45.0)
    parser.add_argument("--stream-timeout", type=float, default=1800.0)
    parser.add_argument("--event-timeout", type=float, default=240.0)
    parser.add_argument("--leave-server-running", action="store_true")
    parser.add_argument("--no-auto-approve-permissions", action="store_true")
    parser.add_argument("--initial-prompt", default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--selection-prompt", default=DEFAULT_SELECTION_PROMPT)
    parser.add_argument("--normal-followup-prompt", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    parser.add_argument("--recovery-prompt", default=DEFAULT_RECOVERY_PROMPT)
    parser.add_argument("--expected-text", default=DEFAULT_NORMAL_FOLLOWUP_PROMPT)
    return parser.parse_args(argv)


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
    try:
        harness.preflight()
        harness.start_server()
        callback(harness)
        return harness.finish()
    except Exception as exc:
        harness.notes.append(f"exception: {type(exc).__name__}: {exc}")
        return harness.finish(passed=False, abort_reason=str(exc))
    finally:
        harness.terminate()


def run_scenario1(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial stream reached input_required"] = _reached_input_required(initial)
        h.checks["initial input_required is step4 confirm_and_select"] = (
            initial.last_input_required_step_id == "confirm_and_select"
        )
        selection = h.stream(prompt=args.selection_prompt, name="02-select-candidate")
        h.checks["selection completed pipeline"] = _pipeline_completed(selection)
        h.checks["selection produced normal handoff"] = selection.normal_handoff_ready
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
        h.checks["recovery answer mentions previous question"] = args.expected_text in recovery.text
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
                timeout=args.event_timeout,
                name_prefix="initial-running",
            )
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for observed_stream in observed_streams:
            _join_after_kill(observed_stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after restart"] = _snapshot(snapshot) is not None
        h.checks["pipeline taskId persisted"] = _snapshot_value(snapshot, "taskId") == h.pipeline_task_id
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="03-continue-after-restart")
        _finish_pipeline_after_possible_input(h, resumed, args)
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
        h.checks["history answer mentions previous question"] = args.normal_followup_prompt in final.text

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
        if answer.last_input_required_step_id:
            second = h.stream(prompt=ASK_SECOND_ANSWER, name="03-answer-second-ask")
            _add_same_task_checks(h, second, "second ask answer")
            _finish_pipeline_after_possible_input(h, second, args)
            final_summary = second
        else:
            _finish_pipeline_after_possible_input(h, answer, args)
        h.checks["pipeline completed after ask recovery"] = _completed_snapshot_or_stream(h, final_summary)
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
        h.checks["selection completed pipeline"] = _pipeline_completed(selection)
        h.checks["VSwitch evidence found"] = _has_any_marker(_all_evidence(h), VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


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
        if answer.last_input_required_step_id:
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
        normal = h.stream_image_text(
            text=args.normal_followup_prompt,
            image_key="normal-followup",
            name="03-normal-followup-image",
            task_id="",
        )
        h.checks["normal image follow-up stayed in same context"] = normal.context_id == h.context_id
        h.checks["normal image follow-up used a new task"] = (
            bool(normal.task_id) and normal.task_id != h.pipeline_task_id
        )
        h.checks["normal image follow-up finished turn"] = _normal_turn_finished(normal)
        h.checks["normal image follow-up produced text"] = bool(normal.text.strip())
        h.kill9_and_restart()
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

    return _run_with_harness(args, scenario, callback)


def run_image_interrupt(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        initial = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        observed_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [initial],
            _candidate_started,
            description="candidate started before image interrupt",
            timeout=args.event_timeout,
            name_prefix="initial-running",
        )
        rollback = h.start_stream_image_text(
            text=ROLLBACK_PROMPT,
            image_key="rollback-interrupt",
            name="02-rollback-image-interrupt",
        )
        _wait_any(
            [*observed_streams, rollback],
            _event_type("rollback_completed"),
            description="image rollback_completed",
            timeout=args.event_timeout,
        )
        streams_to_join = [*observed_streams, rollback]
        _wait_any(
            [*observed_streams, rollback],
            _step_started("intent_parsing"),
            description="post-image-rollback step_started(intent_parsing)",
            timeout=args.event_timeout,
        )
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for stream in streams_to_join:
            _join_after_kill(stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after image interrupt restart"] = _snapshot(snapshot) is not None
        resumed = h.stream(prompt=CONTINUE_PROMPT, name="03-continue-after-restart")
        _finish_pipeline_after_possible_input(h, resumed, args)
        h.checks["pipeline completed after image interrupt recovery"] = _completed_snapshot_or_stream(h, resumed)
        final_state = h.fetch_state("after-image-interrupt-completion")
        final_deploying = _final_deployment_evidence(final_state)
        h.checks["final deploying target is security group"] = _has_any_marker(
            final_deploying,
            SECURITY_GROUP_MARKERS,
        )
        h.checks["final deploying target is not VSwitch"] = not _has_any_marker(final_deploying, VSWITCH_MARKERS)

    return _run_with_harness(args, scenario, callback)


def run_rollback(args: argparse.Namespace, scenario: str) -> int:
    target_step = _ROLLBACK_SCENARIOS[scenario]

    def callback(h: ScenarioHarness) -> None:
        initial = h.start_stream(prompt=args.initial_prompt, name="01-initial-running", context_id="", task_id="")
        observed_streams = _wait_for_with_intervening_ask_inputs(
            h,
            [initial],
            _candidate_started,
            description="candidate started before rollback",
            timeout=args.event_timeout,
            name_prefix="initial-running",
        )
        rollback = h.start_stream(prompt=ROLLBACK_PROMPT, name="02-rollback-interrupt")
        _wait_any(
            [*observed_streams, rollback],
            _event_type("rollback_completed"),
            description="rollback_completed",
            timeout=args.event_timeout,
        )
        streams_to_join = [*observed_streams, rollback]
        if target_step == "deploying":
            _wait_any(
                [*observed_streams, rollback],
                _input_required_step("confirm_and_select"),
                description="post-rollback input_required(confirm_and_select)",
                timeout=args.event_timeout,
            )
            selection = h.start_stream(prompt=args.selection_prompt, name="03-select-after-rollback")
            streams_to_join.append(selection)
            _wait_any(
                [selection],
                _step_started(target_step),
                description=f"post-rollback step_started({target_step})",
                timeout=args.event_timeout,
            )
        else:
            _wait_any(
                [*observed_streams, rollback],
                _step_started(target_step),
                description=f"post-rollback step_started({target_step})",
                timeout=args.event_timeout,
            )
        h.fetch_state("before-kill")
        h.kill9_and_restart()
        for stream in streams_to_join:
            _join_after_kill(stream, h)
        snapshot = h.fetch_state("after-restart")
        h.checks["state endpoint returned snapshot after rollback restart"] = _snapshot(snapshot) is not None
        resumed = h.stream(
            prompt=CONTINUE_PROMPT,
            name="04-continue-after-restart" if target_step == "deploying" else "03-continue-after-restart",
        )
        _finish_pipeline_after_possible_input(h, resumed, args)
        h.checks["pipeline completed after rollback recovery"] = _completed_snapshot_or_stream(h, resumed)
        final_state = h.fetch_state("after-rollback-completion")
        final_deploying = _final_deployment_evidence(final_state)
        h.checks["final deploying target is security group"] = _has_any_marker(
            final_deploying,
            SECURITY_GROUP_MARKERS,
        )
        h.checks["final deploying target is not VSwitch"] = not _has_any_marker(final_deploying, VSWITCH_MARKERS)

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
        final = h.stream(prompt=args.recovery_prompt, name="04-normal-history-check", task_id="")
        h.checks["history answer after cancel mentions previous question"] = args.normal_followup_prompt in final.text

    return _run_with_harness(args, scenario, callback)


def run_fault_after_snapshot(args: argparse.Namespace, scenario: str) -> int:
    def callback(h: ScenarioHarness) -> None:
        if not args.deterministic:
            raise RuntimeError("fault-after-snapshot requires --deterministic")
        initial = BackgroundStream(
            server_url=h.server_url,
            cwd=h.cwd,
            prompt=args.initial_prompt,
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
        initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
        initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
        h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"

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

        rollback = h.start_stream(prompt=ROLLBACK_PROMPT, name="03-rollback-after-first-stack")
        _wait_any(
            [first_deploy, rollback],
            _event_type("rollback_completed"),
            description="rollback_completed after first stack",
            timeout=args.event_timeout,
        )
        _wait_any(
            [first_deploy, rollback],
            _input_required_step("confirm_and_select"),
            description="post-rollback input_required(confirm_and_select)",
            timeout=_post_rollback_timeout(args),
        )
        cleanup_stack_ids = _cleanup_target_stack_ids(h, exclude=set())
        h.checks["rollback cleanup ledger includes first stack"] = bool(first_stack_id) and (
            first_stack_id in cleanup_stack_ids
        )
        h.checks["rollback cleanup target stacks observed"] = bool(cleanup_stack_ids)

        second_deploy = h.start_stream(
            prompt=_cleanup_deployment_prompt(args.selection_prompt, h, "second"),
            name="04-select-second-stack",
        )
        _wait_any(
            [second_deploy],
            _step_started("deploying"),
            description="second deployment step_started(deploying)",
            timeout=args.event_timeout,
        )
        for stream in (first_deploy, rollback, second_deploy):
            _join_stream_or_note(stream, h)

        _finish_pipeline_after_possible_input(h, second_deploy.summary, args)
        h.checks["pipeline completed after second deployment"] = _completed_snapshot_or_stream(h, second_deploy.summary)
        h.fetch_state("after-second-stack")
        second_stack_id = _created_stack_id_from_stream(second_deploy, exclude=set(cleanup_stack_ids))
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
            cleanup_summary = h.stream(
                prompt=args.normal_followup_prompt,
                name="05-cleanup-normal-turn",
                task_id="",
            )
        h.checks["cleanup normal turn stayed in same context"] = cleanup_summary.context_id == h.context_id
        h.checks["cleanup normal turn used normal task"] = cleanup_summary.task_id != h.pipeline_task_id

        after_cleanup = h.fetch_state("after-cleanup")
        cleanup_resource = _cleanup_resource_for_stack(after_cleanup, first_stack_id)
        h.checks["first rollback stack cleanup completed in snapshot"] = _cleanup_resource_completed(cleanup_resource)
        h.checks["rollback cleanup stacks completed in snapshot"] = bool(cleanup_stack_ids) and all(
            _cleanup_resource_completed(_cleanup_resource_for_stack(after_cleanup, stack_id))
            for stack_id in cleanup_stack_ids
        )
        h.checks["cleanup snapshot does not target second stack"] = (
            bool(second_stack_id) and _cleanup_resource_for_stack(after_cleanup, second_stack_id) is None
        )

        ros_stack_ids = _unique_strings([*cleanup_stack_ids, second_stack_id])
        ros_states = _capture_ros_stack_states(
            h,
            ros_stack_ids,
            "after-cleanup",
        )
        h.checks["ROS first rollback stack deleted"] = _ros_stack_deleted(ros_states.get(first_stack_id, {}))
        h.checks["ROS rollback cleanup stacks deleted"] = bool(cleanup_stack_ids) and all(
            _ros_stack_deleted(ros_states.get(stack_id, {})) for stack_id in cleanup_stack_ids
        )
        h.checks["ROS second stack retained"] = bool(second_stack_id) and _ros_stack_retained(
            ros_states.get(second_stack_id, {})
        )

    return _run_with_harness(args, scenario, callback)


def _complete_pipeline(h: ScenarioHarness, args: argparse.Namespace) -> None:
    initial = h.stream(prompt=args.initial_prompt, name="01-initial", context_id="", task_id="")
    initial = _answer_intervening_ask_inputs(h, initial, name_prefix="01-initial")
    h.checks["initial reached step4 selection"] = initial.last_input_required_step_id == "confirm_and_select"
    selection = h.stream(prompt=args.selection_prompt, name="02-select-candidate")
    h.checks["selection completed pipeline"] = _pipeline_completed(selection)
    h.checks["selection produced normal handoff"] = selection.normal_handoff_ready
    h.snapshots["after_pipeline"] = h.fetch_state("after-pipeline")


def _finish_pipeline_after_possible_input(h: ScenarioHarness, summary: StreamSummary, args: argparse.Namespace) -> None:
    current = summary
    for idx in range(1, 5):
        if _pipeline_completed(current):
            return
        if current.last_input_required_step_id == "confirm_and_select":
            current = h.stream(prompt=args.selection_prompt, name=f"select-after-resume-{idx}")
            continue
        if _reached_input_required(current):
            current = h.stream(prompt=CONTINUE_PROMPT, name=f"continue-after-input-{idx}")
            continue
        if current.last_status_state in {"TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
            return
        snapshot = h.fetch_state(f"post-resume-{idx}")
        if _snapshot_value(snapshot, "status") == "completed":
            return
        if (
            _snapshot_value(snapshot, "status") == "waiting_input"
            and _pending_step_id(snapshot) == "confirm_and_select"
        ):
            current = h.stream(prompt=args.selection_prompt, name=f"select-from-snapshot-{idx}")
            continue
        current = h.stream(prompt=CONTINUE_PROMPT, name=f"continue-loop-{idx}")


def _apply_event(summary: StreamSummary, payload: Any) -> None:
    summary.event_count += 1
    identity = _a2a_task_identity(payload)
    if identity is not None:
        summary.task_id = str(identity.get("taskId") or summary.task_id)
        summary.context_id = str(identity.get("contextId") or summary.context_id)
        state = identity.get("state")
        if state:
            summary.status_states.append(str(state))

    envelope = _extract_pipeline_envelope(payload)
    if envelope is not None:
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

    for text in _status_message_texts(payload):
        summary.text += text


def _is_normal_handoff(envelope: dict[str, Any]) -> bool:
    data = envelope.get("data")
    return (
        isinstance(data, dict)
        and (envelope.get("eventType") or envelope.get("event_type")) == "pipeline_handoff_ready"
        and data.get("action") == "switch_to_normal"
        and (data.get("targetMode") or data.get("target_mode")) == "normal"
    )


def _step_started(step_id: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        step = envelope.get("step") if isinstance(envelope, dict) else None
        return (
            isinstance(envelope, dict)
            and envelope.get("eventType") == "step_started"
            and isinstance(step, dict)
            and step.get("id") == step_id
        )

    return predicate


def _candidate_started(event: Any, _summary: StreamSummary) -> bool:
    envelope = _extract_pipeline_envelope(event)
    return isinstance(envelope, dict) and envelope.get("eventType") in {"candidate_started", "candidate_step_started"}


def _input_required_step(step_id: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        step = envelope.get("step") if isinstance(envelope, dict) else None
        data = envelope.get("data") if isinstance(envelope, dict) else None
        data_step_id = data.get("stepId") if isinstance(data, dict) else None
        return (
            isinstance(envelope, dict)
            and envelope.get("eventType") == "input_required"
            and ((isinstance(step, dict) and step.get("id") == step_id) or data_step_id == step_id)
        )

    return predicate


def _event_type(event_type: str) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        return isinstance(envelope, dict) and envelope.get("eventType") == event_type

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
) -> list[BackgroundStream]:
    active_streams = list(streams)
    handled_finished_streams: set[int] = set()
    answered_count = 0
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        for stream in list(active_streams):
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
                if _latest_input_required_kind_from_events(stream.events) != "ask_user_question":
                    continue
                answered_count += 1
                if answered_count > 4:
                    raise RuntimeError(f"too many intervening ask_user_question inputs before {description}") from exc
                h.notes.append(f"answered intervening ask_user_question while waiting for {description}: {stream.name}")
                answer = h.start_stream(
                    prompt=INTERVENING_ASK_ANSWER,
                    name=f"{name_prefix}-answer-ask-{answered_count}",
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


def _answer_intervening_ask_inputs(
    h: ScenarioHarness,
    summary: StreamSummary,
    *,
    name_prefix: str,
) -> StreamSummary:
    current = summary
    for idx in range(1, 5):
        if _pipeline_completed(current) or current.last_input_required_step_id == "confirm_and_select":
            return current
        if not _reached_input_required(current):
            return current
        kind = _latest_pending_kind(h.run_dir / f"{current.name}.events.jsonl")
        if kind != "ask_user_question":
            return current
        h.notes.append(f"answered intervening ask_user_question before step4 selection: {current.name}")
        current = h.stream(prompt=INTERVENING_ASK_ANSWER, name=f"{name_prefix}-answer-ask-{idx}")
    return current


def _join_after_kill(stream: BackgroundStream, h: ScenarioHarness) -> None:
    try:
        stream.join(timeout=10)
    except Exception as exc:
        h.notes.append(f"{stream.name} ended after kill with: {type(exc).__name__}: {exc}")


def _reached_input_required(summary: StreamSummary) -> bool:
    return "TASK_STATE_INPUT_REQUIRED" in summary.status_states or "input_required" in summary.pipeline_event_types


def _pipeline_completed(summary: StreamSummary) -> bool:
    return summary.last_status_state == "TASK_STATE_COMPLETED" or "pipeline_completed" in summary.pipeline_event_types


def _add_same_task_checks(h: ScenarioHarness, summary: StreamSummary, prefix: str) -> None:
    h.checks[f"{prefix} requested recovered taskId"] = summary.request_task_id == h.pipeline_task_id
    h.checks[f"{prefix} stayed in recovered context"] = summary.context_id == h.context_id
    h.checks[f"{prefix} streamed recovered taskId"] = summary.task_id == h.pipeline_task_id


def _add_hydrated_task_checks(h: ScenarioHarness, summary: StreamSummary, prefix: str) -> None:
    h.checks[f"{prefix} omitted taskId"] = summary.request_task_id == ""
    h.checks[f"{prefix} stayed in recovered context"] = summary.context_id == h.context_id
    h.checks[f"{prefix} hydrated recovered taskId"] = summary.task_id == h.pipeline_task_id


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


def _final_deployment_evidence(response: Any) -> str:
    evidence: dict[str, Any] = {"deploying_step": _step_evidence(response, "deploying")}
    handoff_context = _handoff_context(response)
    if isinstance(handoff_context, dict):
        evidence["deployment"] = handoff_context.get("deployment")
    return json.dumps(evidence, ensure_ascii=False, default=str)


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
    end = summary.find("\n\nUse this context", start)
    raw_context = summary[start:] if end < 0 else summary[start:end]
    try:
        value = json.loads(raw_context)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
        envelope = _extract_pipeline_envelope(value)
        if isinstance(envelope, dict) and envelope.get("eventType") == "input_required":
            data = envelope.get("data")
            if isinstance(data, dict):
                kind = str(data.get("kind") or "")
    return kind


def _latest_input_required_kind_from_events(events: Iterable[Any]) -> str:
    kind = ""
    for value in events:
        envelope = _extract_pipeline_envelope(value)
        if isinstance(envelope, dict) and envelope.get("eventType") == "input_required":
            data = envelope.get("data")
            if isinstance(data, dict):
                kind = str(data.get("kind") or "")
    return kind


def _all_evidence(h: ScenarioHarness) -> str:
    return json.dumps(
        {
            "summaries": {name: asdict(summary) for name, summary in h.summaries.items()},
            "snapshots": h.snapshots,
        },
        ensure_ascii=False,
        default=str,
    )


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
    stack_name = _cleanup_stack_name(h, label)
    completion_instruction = (
        "本轮是回滚窗口验证：CreateStack 成功后不要调用 complete_step，不要结束 deploying step；"
        "只简短说明新建的 stack_id，并等待用户下一条指令。"
        if label == "first"
        else "complete_step 前必须在本轮对话中看到一次新的 CreateStack 成功，部署总结的 stack_id 必须来自这次新建。"
    )
    return (
        f"{base_prompt}\n\n"
        "E2E 强制部署约束：\n"
        f"- 本轮唯一成功条件是新建一个 ROS stack，StackName 必须精确等于 `{stack_name}`。\n"
        "- 任何已有 stack（即使是 CREATE_COMPLETE）都必须视为失败结果，不能作为部署成功依据。\n"
        f"- 调用 ros_stack 或 aliyun_api CreateStack 前，必须复核工具参数里的 StackName 精确等于 `{stack_name}`。\n"
        f"- 如果模板、文件名、候选方案或默认值给出了其他 StackName，必须覆盖为 `{stack_name}` 后再调用 CreateStack。\n"
        f"- 如果已经用其他 StackName 调用失败，不能 GetStack 或复用那个 stack，必须改用 `{stack_name}` "
        "重新 CreateStack。\n"
        "- 如果无法使用上述 StackName 新建 stack，就停下来说明失败，不要调用 complete_step。\n"
        f"{completion_instruction}"
        "创建 VSwitch 时请先检查目标 VPC 已有 VSwitch CIDR，选择未占用且属于 VPC CIDR 的网段；"
        "如果 CIDR 冲突，请选择另一个未占用网段并继续使用上述指定 StackName。"
    )


def _cleanup_stack_name(h: ScenarioHarness, label: str) -> str:
    suffix = Path(getattr(h, "run_dir", "")).name.rsplit("-", maxsplit=1)[-1] or "stack"
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
) -> str:
    match = _wait_any(
        [stream],
        _created_stack_event(exclude),
        description="successful CreateStack stack_current_changed",
        timeout=timeout,
    )
    envelope = _extract_pipeline_envelope(match.event)
    data = envelope.get("data") if isinstance(envelope, dict) else None
    stack_id = _string_from_mapping(data, "stackId", "stack_id", "StackId")
    if not stack_id:
        raise RuntimeError("successful CreateStack event did not include a stack id")
    return stack_id


def _created_stack_id_from_stream(stream: Any, *, exclude: set[str]) -> str | None:
    for event in getattr(stream, "events", []) or []:
        envelope = _extract_pipeline_envelope(event)
        if not isinstance(envelope, dict) or envelope.get("eventType") != "stack_current_changed":
            continue
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        if str(data.get("provider") or "").lower() != "ros":
            continue
        if data.get("action") != "CreateStack" or data.get("isSuccess") is not True:
            continue
        stack_id = _string_from_mapping(data, "stackId", "stack_id", "StackId")
        if stack_id and stack_id not in exclude:
            return stack_id
    return None


def _created_stack_event(exclude: set[str]) -> Callable[[Any, StreamSummary], bool]:
    def predicate(event: Any, _summary: StreamSummary) -> bool:
        envelope = _extract_pipeline_envelope(event)
        if not isinstance(envelope, dict) or envelope.get("eventType") != "stack_current_changed":
            return False
        data = envelope.get("data")
        if not isinstance(data, dict):
            return False
        if str(data.get("provider") or "").lower() != "ros":
            return False
        if data.get("action") != "CreateStack" or data.get("isSuccess") is not True:
            return False
        stack_id = _string_from_mapping(data, "stackId", "stack_id", "StackId")
        return bool(stack_id and stack_id not in exclude)

    return predicate


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
        session_dir = SessionStorage().session_dir(cwd, session_id)
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
        envelope = _extract_pipeline_envelope(event)
        if not isinstance(envelope, dict) or envelope.get("eventType") not in event_types:
            return False
        data = envelope.get("data")
        return isinstance(data, dict) and data.get("resourceId") == stack_id

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
        envelope = _extract_pipeline_envelope(value)
        if not isinstance(envelope, dict) or envelope.get("eventType") not in event_types:
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
        envelope = _extract_pipeline_envelope(value)
        if isinstance(envelope, dict) and _pipeline_envelope_has_cleanup_activity(envelope):
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


def _snapshot_cleanup(response: Any) -> dict[str, Any]:
    snapshot = _snapshot(response)
    cleanup = snapshot.get("cleanup") if isinstance(snapshot, dict) else None
    return cleanup if isinstance(cleanup, dict) else {}


def _snapshot_has_cleanup_activity(response: Any) -> bool:
    return _cleanup_payload_has_targets(_snapshot_cleanup(response))


def _cleanup_payload_has_targets(cleanup: dict[str, Any]) -> bool:
    resources = cleanup.get("resources")
    if isinstance(resources, list) and any(isinstance(item, dict) for item in resources):
        return True
    history = cleanup.get("history")
    if isinstance(history, list) and history:
        return True
    resource_count = cleanup.get("resourceCount", cleanup.get("resource_count"))
    if _positive_int(resource_count):
        return True
    status = str(cleanup.get("status") or "")
    return status in CLEANUP_ACTIVE_STATUSES


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
    "fault-after-snapshot",
    "image-ask-waiting",
    "image-initial",
    "image-interrupt",
    "image-normal-handoff",
    "image-selection-waiting",
    "scenario1",
    "normal-running",
    "ask-waiting",
    "selection-waiting",
    "rollback-step5-cleanup",
    "rollback-step5-cleanup-recovery",
    *_RUNNING_STEP_SCENARIOS,
    *_ROLLBACK_SCENARIOS,
    *_CANCEL_SCENARIOS,
}
_SCENARIOS: dict[str, Callable[[argparse.Namespace, str], int]] = {
    "image-ask-waiting": run_image_ask_waiting,
    "image-initial": run_image_initial,
    "image-interrupt": run_image_interrupt,
    "image-normal-handoff": run_image_normal_handoff,
    "image-selection-waiting": run_image_selection_waiting,
    "scenario1": run_scenario1,
    "normal-running": run_normal_running,
    "ask-waiting": run_ask_waiting,
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
