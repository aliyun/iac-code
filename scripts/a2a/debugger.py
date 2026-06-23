from __future__ import annotations

import argparse
import base64
import errno
import html
import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

A2A_VERSION_HEADERS = {"A2A-Version": "1.0"}
DEBUG_LOG_ROOT_NAME = "iac-code-a2a-debugger-runs"
_DEBUG_LOG_LOCK = threading.Lock()
DEBUGGER_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(("image/png", "image/jpeg", "image/webp", "image/gif"))
DEBUGGER_MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class DebuggerConfig:
    host: str
    port: int
    default_server_url: str
    default_cwd: str
    log_dir: str = ""
    replay_export: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProxyResult:
    status_code: int
    data: Any
    text: str
    headers: dict[str, str]
    error: str | None = None


def _decode_json_text(raw: bytes) -> tuple[Any, str]:
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return None, ""
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        return None, text


def _json_for_script(value: Any) -> str:
    return json.dumps(value).replace("</", "<\\/")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_debug_log_dir(root: str | None = None) -> Path:
    root_path = Path(root or tempfile.gettempdir()).expanduser()
    if root is None:
        root_path = root_path / DEBUG_LOG_ROOT_NAME
    run_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    path = root_path / run_name
    path.mkdir(parents=True, exist_ok=False)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local A2A pipeline debugger.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=41880)
    parser.add_argument("--default-server-url", default="http://127.0.0.1:41299")
    parser.add_argument("--default-cwd", default=os.getcwd())
    parser.add_argument(
        "--log-dir",
        default="",
        help="Root directory for per-run debugger JSONL logs. Defaults to the system temp directory.",
    )
    parser.add_argument(
        "--load-log-dir",
        default="",
        help="Load an existing debugger run log directory as a read-only replay page.",
    )
    return parser.parse_args(argv)


def normalize_server_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("serverUrl must be an http or https URL")
    return normalized


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> ProxyResult:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", **A2A_VERSION_HEADERS} if payload is not None else {}
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            data, text = _decode_json_text(raw)
            return ProxyResult(
                status_code=response.status,
                data=data,
                text=text,
                headers=dict(response.headers.items()),
            )
    except HTTPError as exc:
        raw = exc.read()
        data, text = _decode_json_text(raw)
        return ProxyResult(
            status_code=exc.code,
            data=data,
            text=text,
            headers=dict(exc.headers.items()),
            error=f"HTTP {exc.code}",
        )
    except (TimeoutError, URLError, OSError) as exc:
        return ProxyResult(status_code=0, data=None, text="", headers={}, error=str(exc))


def _normalize_image_part(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("images entries must be objects")
    media_type = str(
        raw.get("mediaType") or raw.get("media_type") or raw.get("mimeType") or raw.get("type") or "",
    ).lower()
    if media_type not in DEBUGGER_SUPPORTED_IMAGE_MEDIA_TYPES:
        supported = ", ".join(sorted(DEBUGGER_SUPPORTED_IMAGE_MEDIA_TYPES))
        raise ValueError(f"images must use one of these mediaType values: {supported}")

    encoded = raw.get("bytes") or raw.get("base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("images entries must include base64 bytes")
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("images entries must include valid base64 bytes") from exc
    if len(decoded) > DEBUGGER_MAX_IMAGE_BYTES:
        raise ValueError("images entries must be 5 MiB or smaller")

    filename = os.path.basename(str(raw.get("filename") or raw.get("name") or "image"))
    return {
        "data": {"filename": filename or "image", "bytes": encoded},
        "mediaType": media_type,
    }


def _normalize_image_parts(images: Any) -> list[dict[str, Any]]:
    if images in (None, ""):
        return []
    if not isinstance(images, list):
        raise ValueError("images must be a list")
    return [_normalize_image_part(image) for image in images]


def build_message_stream_payload(
    *,
    cwd: str,
    prompt: str,
    context_id: str,
    task_id: str,
    request_id: str,
    message_id: str,
    images: Any = None,
) -> dict[str, Any]:
    parts = []
    if prompt:
        parts.append({"text": prompt})
    parts.extend(_normalize_image_parts(images))
    message: dict[str, Any] = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": parts,
        "metadata": {"iac_code": {"cwd": cwd}},
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendStreamingMessage",
        "params": {
            "message": message,
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        },
    }


def build_task_cancel_payload(*, task_id: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "CancelTask",
        "params": {"id": task_id},
    }


def build_task_get_payload(
    *,
    task_id: str,
    history_length: int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"id": task_id}
    if history_length is not None:
        params["historyLength"] = history_length
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": "GetTask",
        "params": params,
    }


def _pipeline_from_metadata(metadata: Any) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    iac_code = metadata.get("iac_code") or metadata.get("iacCode") or metadata.get("iac-code")
    if isinstance(iac_code, dict) and isinstance(iac_code.get("pipeline"), dict):
        return iac_code["pipeline"]
    return None


def _extract_pipeline_envelope(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict | list):
        return None
    if isinstance(payload, list):
        for item in payload:
            envelope = _extract_pipeline_envelope(item)
            if envelope is not None:
                return envelope
        return None

    if isinstance(payload.get("eventType") or payload.get("event_type"), str):
        return payload

    for key in ("pipeline", "pipelineEvent", "pipelineSnapshot"):
        if isinstance(payload.get(key), dict):
            return payload[key]

    metadata_envelope = _pipeline_from_metadata(payload.get("metadata"))
    if metadata_envelope is not None:
        return metadata_envelope

    for key in ("result", "params", "task", "statusUpdate", "status_update", "status", "message", "event", "events"):
        envelope = _extract_pipeline_envelope(payload.get(key))
        if envelope is not None:
            return envelope
    return None


def _a2a_task_identity(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict | list):
        return None
    if isinstance(payload, list):
        for item in payload:
            identity = _a2a_task_identity(item)
            if identity is not None:
                return identity
        return None
    if isinstance(payload.get("result"), dict):
        return _a2a_task_identity(payload["result"])

    status_update = payload.get("statusUpdate") or payload.get("status_update")
    if isinstance(status_update, dict):
        status = status_update.get("status") if isinstance(status_update.get("status"), dict) else {}
        return {
            "kind": "status_update",
            "taskId": status_update.get("taskId") or status_update.get("task_id") or "",
            "contextId": status_update.get("contextId") or status_update.get("context_id") or "",
            "state": status.get("state") if isinstance(status, dict) else "",
        }

    task = payload.get("task") if isinstance(payload.get("task"), dict) else payload
    if task.get("id") or task.get("taskId") or task.get("task_id"):
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        return {
            "kind": "task_submitted",
            "taskId": task.get("id") or task.get("taskId") or task.get("task_id") or "",
            "contextId": task.get("contextId") or task.get("context_id") or "",
            "state": status.get("state") if isinstance(status, dict) else "",
        }

    for key in ("event", "events"):
        identity = _a2a_task_identity(payload.get(key))
        if identity is not None:
            return identity
    return None


def _parsed_debug_event_type(raw: Any, fallback: str) -> str:
    if isinstance(raw, dict):
        envelope = _extract_pipeline_envelope(raw)
        if envelope is not None:
            event_type = envelope.get("eventType") or envelope.get("event_type")
            if event_type:
                return str(event_type)
        if "health" in raw and "agentCard" in raw:
            return "health"
        raw_type = raw.get("type")
        if raw_type:
            return str(raw_type)
        identity = _a2a_task_identity(raw)
        if identity is not None:
            return str(identity.get("kind") or fallback)
    return fallback


def _debug_log_record(kind: str, raw: Any) -> dict[str, Any]:
    envelope = _extract_pipeline_envelope(raw)
    identity = _a2a_task_identity(raw)
    return {
        "receivedAt": _utc_now(),
        "kind": kind,
        "parsedEventType": _parsed_debug_event_type(raw, kind),
        "taskId": (envelope or {}).get("taskId") or (identity or {}).get("taskId") or "",
        "contextId": (envelope or {}).get("contextId") or (identity or {}).get("contextId") or "",
        "sequence": (envelope or {}).get("sequence"),
        "raw": raw,
    }


def _debug_log_filename(kind: str) -> str:
    return {
        "request": "requests.jsonl",
        "snapshot": "snapshots.jsonl",
        "error": "errors.jsonl",
    }.get(kind, "sse-events.jsonl")


def append_debug_log(config: DebuggerConfig, kind: str, raw: Any) -> None:
    if not config.log_dir:
        return
    path = Path(config.log_dir).expanduser()
    record = _debug_log_record(kind, raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
        with _DEBUG_LOG_LOCK:
            with (path / _debug_log_filename(kind)).open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
    except (OSError, TypeError, ValueError):
        return


def load_debug_log_export(log_dir: str | Path) -> dict[str, Any]:
    path = Path(log_dir).expanduser()
    sse_events = _load_debug_log_raw_values(path / "sse-events.jsonl")
    snapshots = _load_debug_log_raw_values(path / "snapshots.jsonl")
    requests = _load_debug_log_raw_values(path / "requests.jsonl")
    latest_snapshot = snapshots[-1] if snapshots else None
    snapshot_events = [
        event
        for snapshot in snapshots
        if isinstance(snapshot, dict) and isinstance(snapshot.get("events"), list)
        for event in snapshot["events"]
    ]
    replay_events = [*sse_events, *snapshot_events]
    latest_pipeline = None
    active_task_id = ""
    task_history: dict[str, dict[str, str]] = {}
    last_sequence = 0

    def sequence_value(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return int(value)

    def terminal_pipeline_status(value: Any) -> bool:
        state = str(value or "").lower().replace("task_state_", "").replace("-", "_")
        return state in {"canceled", "cancelled", "failed", "denied", "completed"}

    def remember_task(*, task_id: Any, context_id: Any = "", state: Any = "", role: str = "active") -> None:
        normalized_task_id = str(task_id or "")
        if not normalized_task_id:
            return
        existing = task_history.get(normalized_task_id, {})
        task_history[normalized_task_id] = {
            "taskId": normalized_task_id,
            "contextId": str(context_id or existing.get("contextId") or ""),
            "state": str(state or existing.get("state") or ""),
            "role": role or existing.get("role") or "active",
        }

    for item in replay_events:
        identity = _a2a_task_identity(item)
        if identity is not None:
            active_task_id = str(identity.get("taskId") or active_task_id)
            remember_task(
                task_id=identity.get("taskId"),
                context_id=identity.get("contextId"),
                state=identity.get("state"),
                role="active",
            )
        envelope = _extract_pipeline_envelope(item)
        if envelope is None:
            continue
        latest_pipeline = envelope
        remember_task(
            task_id=envelope.get("taskId"),
            context_id=envelope.get("contextId"),
            state=envelope.get("state") or envelope.get("status"),
            role="pipeline",
        )
        sequence = sequence_value(envelope.get("sequence"))
        if sequence is not None:
            last_sequence = max(last_sequence, sequence)
        if terminal_pipeline_status(envelope.get("state") or envelope.get("status")) and active_task_id == str(
            envelope.get("taskId") or ""
        ):
            active_task_id = ""
    return {
        "schemaVersion": "iac-code-a2a-debugger-export-v1",
        "exportedAt": _utc_now(),
        "connection": {},
        "task": {
            "taskId": (latest_pipeline or {}).get("taskId", ""),
            "activeTaskId": active_task_id,
            "contextId": (latest_pipeline or {}).get("contextId", ""),
            "status": (latest_pipeline or {}).get("status", "snapshot"),
            "lastSequence": last_sequence,
            "recovery": "log_replay",
        },
        "taskHistory": list(task_history.values()),
        "waitingInput": "",
        "latestPermission": None,
        "snapshot": latest_snapshot,
        "sseEvents": replay_events,
        "requests": requests,
        "executionTree": {"rootIds": [], "nodes": {}},
        "uiState": {},
    }


def _load_debug_log_raw_values(path: Path) -> list[Any]:
    values: list[Any] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "raw" in record:
            values.append(record["raw"])
    return values


def _parse_sse_data_line(raw_line: bytes) -> Any | None:
    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line.startswith("data:"):
        return None
    raw = line[5:].strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def render_index_html(config: DebuggerConfig) -> str:
    default_url = html.escape(config.default_server_url, quote=True)
    default_cwd = html.escape(config.default_cwd, quote=True)
    debug_log_dir = html.escape(config.log_dir or "Debug logging disabled", quote=True)
    defaults_json = _json_for_script(
        {
            "serverUrl": config.default_server_url,
            "cwd": config.default_cwd,
            "debugLogDir": config.log_dir,
        }
    )
    replay_json = _json_for_script(config.replay_export)
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>iac-code A2A Pipeline Debugger</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #172033;
      --muted: #657084;
      --line: #d7dee8;
      --panel: #ffffff;
      --surface: #f5f7fb;
      --accent: #0f8b8d;
      --accent-strong: #0b6b6d;
      --warning: #b06a00;
      --danger: #b3261e;
      --title: #111827;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--surface);
      color: var(--ink);
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }

    button,
    input,
    textarea {
      font: inherit;
    }

    button {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      background: #ffffff;
      color: var(--ink);
      cursor: pointer;
    }

    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #ffffff;
    }

    button.primary:hover {
      background: var(--accent-strong);
    }

    button.danger {
      border-color: #e7b6b2;
      color: var(--danger);
    }

    input,
    textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
      color: var(--ink);
      padding: 9px 10px;
      outline: none;
    }

    input[type="file"] {
      padding: 7px 10px;
    }

    input:focus,
    textarea:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(15, 139, 141, 0.14);
    }

    textarea {
      min-height: 86px;
      resize: vertical;
    }

    .titlebar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      min-height: 64px;
      padding: 0 24px;
      background: var(--title);
      color: #ffffff;
    }

    .titlebar h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .titlebar .subtitle {
      color: #b9c2d0;
      font-size: 13px;
    }

    .controls {
      display: grid;
      gap: 14px;
      padding: 18px 24px;
      background: #ffffff;
      border-bottom: 1px solid var(--line);
    }

    .control-grid {
      display: grid;
      grid-template-columns: minmax(220px, 1.3fr) minmax(180px, 0.9fr) repeat(3, minmax(150px, 0.75fr));
      gap: 12px;
    }

    label {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      letter-spacing: 0;
    }

    .prompt-row {
      display: grid;
      grid-template-columns: minmax(260px, 1fr) auto;
      align-items: end;
      gap: 12px;
    }

    .prompt-stack {
      display: grid;
      gap: 10px;
    }

    .image-summary {
      min-height: 18px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .debug-log-line {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 28px;
      color: var(--muted);
      font-size: 12px;
    }

    .debug-log-line code {
      color: var(--ink);
      overflow-wrap: anywhere;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    }

    .task-history {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      min-height: 28px;
      color: var(--muted);
      font-size: 12px;
    }

    .task-chip {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      min-height: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0 8px;
      background: #f8fafc;
      color: #334155;
      overflow-wrap: anywhere;
    }

    .button-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .main-grid {
      display: grid;
      grid-template-columns: minmax(340px, 1fr) minmax(320px, 0.9fr);
      gap: 16px;
      padding: 16px 24px 24px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 45px;
      padding: 0 14px;
      border-bottom: 1px solid var(--line);
    }

    .panel-header h2 {
      margin: 0;
      font-size: 14px;
      font-weight: 750;
      letter-spacing: 0;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }

    .metric {
      min-height: 72px;
      padding: 12px;
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }

    .metric strong {
      display: block;
      margin-top: 8px;
      overflow-wrap: anywhere;
      font-size: 18px;
      line-height: 1.15;
    }

    #structured-pipeline {
      display: block;
      padding: 14px;
      min-height: 350px;
    }

    .execution-tree {
      display: grid;
      gap: 8px;
    }

    .tree-node {
      border-left: 2px solid #c7d2fe;
      padding-left: 10px;
    }

    .tree-node + .tree-node {
      margin-top: 8px;
    }

    .tree-node[open] > .tree-summary {
      background: #f8fafc;
    }

    .tree-node-pipeline {
      border-left-color: #0f766e;
    }

    .tree-node-step {
      border-left-color: #2563eb;
    }

    .tree-node-normal-chat {
      border-left-color: #0891b2;
    }

    .tree-node-normal-chat > .tree-summary {
      background: #ecfeff;
      border: 1px solid #bae6fd;
    }

    .parallel-group {
      border-left-color: #7c3aed;
      margin-top: 8px;
    }

    .candidate-lane {
      border-left-color: #ea580c;
      min-width: 0;
    }

    .candidate-lanes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 12px;
      align-items: start;
    }

    .candidate-lane > .tree-summary {
      align-items: flex-start;
      flex-wrap: wrap;
      background: #fff7ed;
      border: 1px solid #fed7aa;
    }

    .candidate-lane .tree-title {
      flex-basis: 100%;
      white-space: normal;
    }

    .candidate-lane .tree-meta {
      flex: 1 1 220px;
      min-width: 0;
      white-space: normal;
      overflow-wrap: anywhere;
    }

    .tree-summary {
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 34px;
      border-radius: 8px;
      padding: 6px 8px;
      list-style: none;
    }

    .tree-summary::-webkit-details-marker {
      display: none;
    }

    .tree-title {
      min-width: 0;
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 13px;
      font-weight: 760;
    }

    .tree-meta {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .tree-node-body {
      display: grid;
      gap: 8px;
      padding: 4px 0 2px 10px;
    }

    .tree-node-children {
      display: grid;
      gap: 8px;
    }

    .tree-node-timeline {
      display: grid;
      gap: 6px;
    }

    .timeline-item {
      display: grid;
      gap: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #ffffff;
    }

    .timeline-item-title {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
      font-weight: 760;
    }

    .timeline-item-actions {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-left: auto;
    }

    .timeline-item-meta {
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }

    .timeline-details-button {
      min-height: 22px;
      padding: 0 7px;
      border-radius: 5px;
      border: 1px solid var(--line);
      background: #ffffff;
      color: #334155;
      font-size: 11px;
      font-weight: 700;
    }

    .timeline-detail-body {
      display: none;
      margin-top: 6px;
      border-top: 1px solid var(--line);
      padding-top: 6px;
    }

    .timeline-detail-body.is-open {
      display: block;
    }

    .timeline-detail-body pre {
      max-height: 360px;
      overflow: auto;
      margin: 0;
      border-radius: 6px;
      background: #0f172a;
      color: #e2e8f0;
      padding: 10px;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .timeline-item-text {
      color: #334155;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .timeline-text {
      border-color: #bae6fd;
      background: #f0f9ff;
    }

    .timeline-tool {
      border-color: #bbf7d0;
      background: #f0fdf4;
    }

    .timeline-permission {
      border-color: #fed7aa;
      background: #fff7ed;
    }

    .timeline-rollback {
      border-color: #fecaca;
      background: #fef2f2;
    }

    .timeline-canceled {
      border-color: #fecaca;
      background: #fff1f2;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 0 8px;
      background: #e8f4f4;
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
    }

    .empty {
      display: grid;
      place-items: center;
      min-height: 220px;
      color: var(--muted);
      text-align: center;
    }

    #waiting-input {
      min-height: 98px;
      padding: 14px;
      color: var(--muted);
      white-space: pre-wrap;
    }

    .raw-tabs {
      display: flex;
      gap: 4px;
    }

    .raw-tab {
      min-height: 31px;
      padding: 0 9px;
      border-radius: 5px;
      color: var(--muted);
    }

    .raw-tab[aria-selected="true"] {
      border-color: var(--accent);
      color: var(--accent-strong);
      background: #e8f4f4;
    }

    .raw-output {
      min-height: 492px;
      max-height: 62vh;
      margin: 0;
      padding: 14px;
      overflow: auto;
      background: #0f172a;
      color: #dbeafe;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.55;
    }

    .raw-empty {
      display: grid;
      min-height: 220px;
      place-items: center;
      color: #93a4bd;
    }

    .raw-json,
    .raw-event-body pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: inherit;
    }

    .raw-event-list {
      display: grid;
      gap: 8px;
    }

    .raw-event {
      border: 1px solid rgba(148, 163, 184, 0.35);
      border-radius: 6px;
      background: rgba(15, 23, 42, 0.74);
      overflow: hidden;
    }

    .raw-event summary {
      display: grid;
      grid-template-columns: minmax(130px, 0.7fr) minmax(140px, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 9px 10px;
      cursor: pointer;
      list-style: none;
    }

    .raw-event summary::-webkit-details-marker {
      display: none;
    }

    .raw-event-kind {
      color: #a7f3d0;
      font-weight: 750;
      overflow-wrap: anywhere;
    }

    .raw-event-text {
      color: #dbeafe;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .raw-event-meta {
      color: #93a4bd;
      font-size: 11px;
      white-space: nowrap;
    }

    .raw-event-body {
      display: grid;
      gap: 10px;
      padding: 10px;
      border-top: 1px solid rgba(148, 163, 184, 0.25);
      background: rgba(2, 6, 23, 0.58);
    }

    .raw-event-body-label {
      color: #93a4bd;
      font-size: 11px;
      font-weight: 750;
      text-transform: uppercase;
    }

    .raw-event-text-body {
      color: #f8fafc;
    }

    .raw-event-permission summary {
      background: rgba(176, 106, 0, 0.16);
    }

    .raw-event-request summary {
      background: rgba(14, 116, 144, 0.12);
    }

    .raw-event-ok summary {
      background: rgba(22, 101, 52, 0.12);
    }

    .raw-event-error summary {
      background: rgba(179, 38, 30, 0.16);
    }

    .raw-output.state-raw-output {
      background: var(--surface);
      color: var(--ink);
      font-family:
        Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 13px;
      line-height: 1.45;
    }

    .state-view {
      display: grid;
      gap: 12px;
    }

    .state-summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
      gap: 8px;
    }

    .state-metric,
    .state-section,
    .state-row,
    .state-info {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
    }

    .state-metric {
      min-height: 64px;
      padding: 10px;
    }

    .state-metric span,
    .state-info span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 750;
    }

    .state-metric strong,
    .state-info strong {
      display: block;
      margin-top: 5px;
      overflow-wrap: anywhere;
    }

    .state-section {
      overflow: hidden;
    }

    .state-section-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 40px;
      padding: 0 12px;
      list-style: none;
      cursor: pointer;
    }

    .state-section-header::-webkit-details-marker {
      display: none;
    }

    .state-section[open] > .state-section-header {
      border-bottom: 1px solid var(--line);
    }

    .state-section-header::before {
      flex: 0 0 auto;
      width: 14px;
      color: var(--muted);
      content: "+";
      font-weight: 800;
    }

    .state-section[open] > .state-section-header::before {
      content: "-";
    }

    .state-section-header h3 {
      margin: 0;
      font-size: 13px;
      font-weight: 760;
    }

    .state-section-body {
      display: grid;
      gap: 8px;
      padding: 12px;
    }

    .state-tree {
      display: grid;
      gap: 8px;
    }

    .state-node {
      border-left: 2px solid #c7d2fe;
      padding-left: 10px;
    }

    .state-node[open] > .state-node-summary {
      background: #f8fafc;
    }

    .state-node-pipeline {
      border-left-color: #0f766e;
    }

    .state-node-step {
      border-left-color: #2563eb;
    }

    .state-node-candidate {
      border-left-color: #ea580c;
    }

    .state-node-candidate-step {
      border-left-color: #7c3aed;
    }

    .state-node-summary,
    .state-row summary {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      border-radius: 7px;
      padding: 6px 8px;
      list-style: none;
      cursor: pointer;
    }

    .state-node-summary::-webkit-details-marker,
    .state-row summary::-webkit-details-marker {
      display: none;
    }

    .state-title,
    .state-row-title {
      min-width: 0;
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 750;
    }

    .state-meta,
    .state-row-meta {
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }

    .state-node-body {
      display: grid;
      gap: 8px;
      padding: 4px 0 4px 10px;
    }

    .state-candidate-lanes {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 8px;
      align-items: start;
    }

    .state-info-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 8px;
    }

    .state-info {
      padding: 9px;
      background: #fbfcfe;
    }

    .state-row-list {
      display: grid;
      gap: 8px;
    }

    .state-row {
      overflow: hidden;
    }

    .state-json {
      max-height: 340px;
      overflow: auto;
      margin: 0;
      border-top: 1px solid var(--line);
      background: #0f172a;
      color: #e2e8f0;
      padding: 10px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .state-empty {
      display: grid;
      min-height: 112px;
      place-items: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      color: var(--muted);
      text-align: center;
    }

    @media (max-width: 980px) {
      .control-grid,
      .prompt-row,
      .main-grid,
      .metrics {
        grid-template-columns: 1fr;
      }

      .titlebar {
        align-items: flex-start;
        flex-direction: column;
        padding: 16px;
      }

      .controls,
      .main-grid {
        padding-left: 16px;
        padding-right: 16px;
      }
    }
  </style>
</head>
<body>
  <main id="app">
    <header class="titlebar">
      <h1>iac-code A2A Pipeline Debugger</h1>
      <div class="subtitle">Local browser console for A2A pipeline metadata, snapshots, and SSE traces</div>
    </header>

    <section class="controls" aria-label="Debugger controls">
      <div class="control-grid">
        <label for="server-url">A2A Server URL
          <input id="server-url" value="__DEFAULT_SERVER_URL__" autocomplete="off">
        </label>
        <label for="cwd">cwd
          <input id="cwd" value="__DEFAULT_CWD__" autocomplete="off">
        </label>
        <label for="context-id">contextId
          <input id="context-id" autocomplete="off">
        </label>
        <label for="task-id">Pipeline taskId
          <input id="task-id" autocomplete="off">
        </label>
        <label for="active-task-id">Active taskId
          <input id="active-task-id" autocomplete="off">
        </label>
      </div>
      <div class="debug-log-line">
        <span>Debug Log</span>
        <code id="debug-log-dir">__DEBUG_LOG_DIR__</code>
      </div>
      <div id="task-history" class="task-history" aria-label="Task history"></div>
      <div class="prompt-row">
        <div class="prompt-stack">
          <label for="prompt">Prompt
            <textarea id="prompt" spellcheck="false"></textarea>
          </label>
          <label for="image-input">Images
            <input id="image-input" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple>
          </label>
          <div id="image-summary" class="image-summary">No images selected.</div>
        </div>
        <div class="button-row" aria-label="Actions">
          <button id="health-button" type="button">Health</button>
          <button id="stream-button" class="primary" type="button">Stream</button>
          <button id="fetch-state-button" type="button">Fetch State</button>
          <button id="export-html-button" type="button">Export HTML</button>
          <button id="cancel-button" class="danger" type="button">Cancel</button>
        </div>
      </div>
    </section>

    <section class="main-grid">
      <div>
        <section class="metrics" aria-label="Pipeline metrics">
          <div class="metric"><span>Status</span><strong id="metric-status">idle</strong></div>
          <div class="metric"><span>Pipeline Task</span><strong id="metric-pipeline-task">-</strong></div>
          <div class="metric"><span>Active Task</span><strong id="metric-active-task">-</strong></div>
          <div class="metric"><span>Seq</span><strong id="metric-seq">0</strong></div>
          <div class="metric"><span>Recovery</span><strong id="metric-recovery">-</strong></div>
        </section>

        <section class="panel" aria-label="Structured pipeline">
          <div class="panel-header">
            <h2>Structured Pipeline</h2>
            <span class="pill" id="pipeline-count">0 stages</span>
          </div>
          <div id="structured-pipeline"></div>
        </section>

        <section class="panel" aria-label="Waiting input" style="margin-top: 16px;">
          <div class="panel-header"><h2>Waiting Input</h2></div>
          <div id="waiting-input">No pending input.</div>
        </section>
      </div>

      <section class="panel" aria-label="Raw event panels">
        <div class="panel-header">
          <h2>Raw</h2>
          <div class="raw-tabs" role="tablist" aria-label="Raw views">
            <button class="raw-tab" type="button" data-raw-tab="sse" aria-selected="true">SSE Events</button>
            <button class="raw-tab" type="button" data-raw-tab="snapshot" aria-selected="false">Snapshot</button>
            <button class="raw-tab" type="button" data-raw-tab="requests" aria-selected="false">Requests</button>
          </div>
        </div>
        <div id="raw-output" class="raw-output"></div>
      </section>
    </section>
  </main>
  <script>
    window.DEBUGGER_DEFAULTS = __DEFAULTS_JSON__;
    window.DEBUGGER_REPLAY_DATA = __REPLAY_JSON__;
    const exportPayload = window.DEBUGGER_EXPORT_DATA || window.DEBUGGER_REPLAY_DATA || null;
    const isExportMode = Boolean(exportPayload);

    const state = {
      activeRawTab: "sse",
      status: "idle",
      streamsInFlight: 0,
      taskId: "",
      activeTaskId: "",
      taskHistory: [],
      contextId: "",
      lastSequence: 0,
      recovery: "",
      latestPermission: null,
      stages: [],
      executionTree: createExecutionTree(),
      expandedTreeKeys: new Set(),
      expandedTimelineKeys: new Set(),
      expandedRawEventKeys: new Set(),
      expandedStateSectionKeys: new Set(),
      collapsedStateSectionKeys: new Set(),
      rawPipelineEventKeys: new Set(),
      waitingInput: "",
      normalHandoffReady: false,
      raw: {
        sse: [],
        snapshot: null,
        requests: []
      }
    };

    const byId = (id) => document.getElementById(id);
    const supportedImageMediaTypes = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
    const maxImageBytes = 5 * 1024 * 1024;

    function createExecutionTree() {
      return {
        nodes: {},
        rootIds: [],
        touchedKeys: new Set(),
        textGroups: {},
        normalMessageGroups: {},
        lastStepKey: "",
        lastCandidateKey: "",
        lastCandidateStepKey: ""
      };
    }

    function asPrettyJson(value) {
      if (value === undefined || value === null || value === "") {
        return "";
      }
      if (typeof value === "string") {
        return value;
      }
      return JSON.stringify(value, null, 2);
    }

    function pipelineEventDedupKey(payload) {
      const envelope = extractPipelineEnvelope(rawItemValue(payload));
      if (!envelope || typeof envelope !== "object") {
        return "";
      }
      const eventId = envelope.eventId || envelope.event_id || "";
      if (eventId) {
        return `event:${eventId}`;
      }
      const sequence = envelope.sequence || envelope.seq || "";
      if (!sequence) {
        return "";
      }
      return [
        "sequence",
        envelope.taskId || envelope.task_id || state.taskId || "",
        envelope.contextId || envelope.context_id || state.contextId || "",
        envelope.eventType || envelope.event_type || "",
        sequence
      ].join("|");
    }

    function rememberPipelineEvent(payload) {
      const dedupKey = pipelineEventDedupKey(payload);
      if (!dedupKey) {
        return true;
      }
      if (state.rawPipelineEventKeys.has(dedupKey)) {
        return false;
      }
      state.rawPipelineEventKeys.add(dedupKey);
      return true;
    }

    function resetPipelineEventDedup() {
      state.rawPipelineEventKeys = new Set();
      state.raw.sse.forEach((row) => {
        const dedupKey = pipelineEventDedupKey(row);
        if (dedupKey) {
          state.rawPipelineEventKeys.add(dedupKey);
        }
      });
    }

    function dedupeRawSseEvents(rows) {
      const seen = new Set();
      const result = [];
      rows.forEach((row) => {
        const dedupKey = pipelineEventDedupKey(row);
        if (dedupKey && seen.has(dedupKey)) {
          return;
        }
        if (dedupKey) {
          seen.add(dedupKey);
        }
        result.push(row);
      });
      return result;
    }

    function readControls() {
      return {
        serverUrl: byId("server-url").value.trim(),
        cwd: byId("cwd").value.trim(),
        contextId: byId("context-id").value.trim(),
        taskId: byId("task-id").value.trim(),
        activeTaskId: byId("active-task-id").value.trim(),
        prompt: byId("prompt").value
      };
    }

    function selectedImageFiles() {
      const input = byId("image-input");
      if (!input || !input.files) {
        return [];
      }
      return Array.from(input.files);
    }

    function formatBytes(value) {
      const bytes = Number(value) || 0;
      if (bytes < 1024) {
        return `${bytes} B`;
      }
      if (bytes < 1024 * 1024) {
        return `${(bytes / 1024).toFixed(1)} KiB`;
      }
      return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
    }

    function updateImageSummary() {
      const summary = byId("image-summary");
      if (!summary) {
        return;
      }
      const files = selectedImageFiles();
      if (!files.length) {
        summary.textContent = "No images selected.";
        return;
      }
      summary.textContent = files
        .map((file) => `${file.name || "image"} (${formatBytes(file.size)})`)
        .join(", ");
    }

    function readFileAsDataUrl(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.addEventListener("load", () => resolve(String(reader.result || "")));
        reader.addEventListener("error", () => reject(reader.error || new Error("Failed to read image file.")));
        reader.readAsDataURL(file);
      });
    }

    function imageBase64FromDataUrl(dataUrl) {
      const commaIndex = dataUrl.indexOf(",");
      if (commaIndex < 0) {
        throw new Error("Image file did not produce a valid data URL.");
      }
      return dataUrl.slice(commaIndex + 1);
    }

    async function readSelectedImages() {
      const files = selectedImageFiles();
      const images = [];
      for (const file of files) {
        const mediaType = String(file.type || "").toLowerCase();
        if (!supportedImageMediaTypes.has(mediaType)) {
          throw new Error(`${file.name || "Selected image"} uses unsupported image type ${mediaType || "unknown"}.`);
        }
        if (file.size > maxImageBytes) {
          throw new Error(`${file.name || "Selected image"} is larger than 5 MiB.`);
        }
        const dataUrl = await readFileAsDataUrl(file);
        images.push({
          filename: file.name || "image",
          mediaType,
          bytes: imageBase64FromDataUrl(dataUrl)
        });
      }
      return images;
    }

    function appendRawEvent(kind, value) {
      let row = null;
      if (kind === "snapshot") {
        state.raw.snapshot = value;
      } else if (kind === "request") {
        row = {
          at: new Date().toISOString(),
          status: "sent",
          statusCode: "",
          error: "",
          value
        };
        state.raw.requests.push(row);
      } else {
        const dedupKey = pipelineEventDedupKey(value);
        if (dedupKey && state.rawPipelineEventKeys.has(dedupKey)) {
          return null;
        }
        if (dedupKey) {
          state.rawPipelineEventKeys.add(dedupKey);
        }
        row = {
          at: new Date().toISOString(),
          value
        };
        state.raw.sse.push(row);
      }
      renderRaw();
      return row;
    }

    function updateRawRequest(row, changes) {
      if (!row) {
        return;
      }
      Object.assign(row, changes || {});
      renderRaw();
    }

    function compactText(value, maxLength = 120) {
      const text = String(value || "").replace(/\\s+/g, " ").trim();
      if (text.length <= maxLength) {
        return text;
      }
      return `${text.slice(0, maxLength - 3)}...`;
    }

    function errorMessage(error) {
      if (!error) {
        return "Unknown error";
      }
      if (typeof error === "string") {
        return error;
      }
      if (error.error) {
        return String(error.error);
      }
      if (error.message) {
        return String(error.message);
      }
      if (error.body && typeof error.body === "object" && error.body.error) {
        return String(error.body.error);
      }
      try {
        return JSON.stringify(error);
      } catch {
        return String(error);
      }
    }

    function pipelineFromMetadata(metadata) {
      if (!metadata || typeof metadata !== "object") {
        return null;
      }
      const iacCode = metadata.iac_code || metadata.iacCode || metadata["iac-code"];
      if (iacCode && typeof iacCode === "object" && iacCode.pipeline) {
        return iacCode.pipeline;
      }
      return null;
    }

    function extractPipelineEnvelope(payload) {
      if (!payload || typeof payload !== "object") {
        return null;
      }
      if (Array.isArray(payload)) {
        for (const item of payload) {
          const envelope = extractPipelineEnvelope(item);
          if (envelope) {
            return envelope;
          }
        }
        return null;
      }
      if (payload.pipeline || payload.pipelineEvent || payload.pipelineSnapshot) {
        return payload.pipeline || payload.pipelineEvent || payload.pipelineSnapshot;
      }
      const metadataEnvelope = pipelineFromMetadata(payload.metadata);
      if (metadataEnvelope) {
        return metadataEnvelope;
      }
      if (payload.result) {
        return extractPipelineEnvelope(payload.result);
      }
      if (payload.params) {
        return extractPipelineEnvelope(payload.params);
      }
      if (payload.task) {
        return extractPipelineEnvelope(payload.task);
      }
      if (payload.statusUpdate) {
        return extractPipelineEnvelope(payload.statusUpdate);
      }
      if (payload.status_update) {
        return extractPipelineEnvelope(payload.status_update);
      }
      if (payload.status) {
        return extractPipelineEnvelope(payload.status);
      }
      if (payload.message) {
        return extractPipelineEnvelope(payload.message);
      }
      if (payload.event) {
        return extractPipelineEnvelope(payload.event);
      }
      if (payload.events) {
        return extractPipelineEnvelope(payload.events);
      }
      return null;
    }

    function a2aTaskIdentity(payload) {
      if (!payload || typeof payload !== "object") {
        return null;
      }
      if (Array.isArray(payload)) {
        for (const item of payload) {
          const identity = a2aTaskIdentity(item);
          if (identity) {
            return identity;
          }
        }
        return null;
      }
      if (payload.result) {
        return a2aTaskIdentity(payload.result);
      }
      const statusUpdate = payload.statusUpdate || payload.status_update;
      if (statusUpdate && typeof statusUpdate === "object") {
        const status = statusUpdate.status && typeof statusUpdate.status === "object" ? statusUpdate.status : {};
        return {
          kind: "status_update",
          taskId: statusUpdate.taskId || statusUpdate.task_id || "",
          contextId: statusUpdate.contextId || statusUpdate.context_id || "",
          state: status.state || ""
        };
      }
      const task = payload.task && typeof payload.task === "object" ? payload.task : payload;
      if (task.id || task.taskId || task.task_id) {
        const status = task.status && typeof task.status === "object" ? task.status : {};
        return {
          kind: "task_submitted",
          taskId: task.id || task.taskId || task.task_id || "",
          contextId: task.contextId || task.context_id || "",
          state: status.state || ""
        };
      }
      if (payload.event) {
        return a2aTaskIdentity(payload.event);
      }
      if (payload.events) {
        return a2aTaskIdentity(payload.events);
      }
      return null;
    }

    function recordTaskIdentity(identity, role = "active") {
      const taskId = String(identity && (identity.taskId || identity.task_id || identity.id) || "");
      if (!taskId) {
        return null;
      }
      const contextId = String(identity && (identity.contextId || identity.context_id || state.contextId) || "");
      const taskState = String(identity && (identity.state || identity.status || "") || "");
      const existing = state.taskHistory.find((item) => item.taskId === taskId);
      const next = {
        taskId,
        contextId,
        state: taskState || (existing && existing.state) || "",
        role: role || (existing && existing.role) || "active"
      };
      if (existing) {
        Object.assign(existing, next);
        return existing;
      }
      state.taskHistory.push(next);
      return next;
    }

    function renderTaskHistory() {
      const container = byId("task-history");
      if (!container) {
        return;
      }
      container.textContent = "";
      if (state.taskHistory.length === 0) {
        container.textContent = "No task captured yet.";
        return;
      }
      state.taskHistory.forEach((task) => {
        const chip = document.createElement("span");
        chip.className = "task-chip";
        chip.textContent = [
          task.role || "task",
          compactText(task.taskId, 36),
          task.state || ""
        ].filter(Boolean).join(" · ");
        chip.title = [
          `taskId: ${task.taskId}`,
          task.contextId ? `contextId: ${task.contextId}` : "",
          task.state ? `state: ${task.state}` : "",
          task.role ? `role: ${task.role}` : ""
        ].filter(Boolean).join("\\n");
        container.appendChild(chip);
      });
    }

    function isWorkingA2ATaskState(value) {
      const stateValue = String(value || "").toUpperCase();
      return stateValue === "TASK_STATE_SUBMITTED" || stateValue === "TASK_STATE_WORKING";
    }

    function isTerminalPipelineTaskState(value) {
      const stateValue = String(value || "")
        .toLowerCase()
        .replace(/^task_state_/, "")
        .replace(/-/g, "_");
      return ["canceled", "cancelled", "failed", "denied", "completed"].includes(stateValue);
    }

    function shouldKeepActiveTaskId(identity) {
      return identity && isWorkingA2ATaskState(identity.state);
    }

    function captureA2ATaskIdentity(payload) {
      const identity = a2aTaskIdentity(payload);
      if (!identity) {
        return null;
      }
      const taskId = String(identity.taskId || "");
      if (taskId && shouldKeepActiveTaskId(identity)) {
        state.activeTaskId = taskId;
      } else if (taskId && state.activeTaskId === taskId) {
        state.activeTaskId = "";
      }
      if (!state.taskId && taskId && !state.normalHandoffReady) {
        state.taskId = taskId;
      }
      state.contextId = String(identity.contextId || state.contextId || "");
      recordTaskIdentity(
        {...identity, taskId: taskId || state.activeTaskId || state.taskId, contextId: state.contextId},
        state.taskId && taskId === state.taskId ? "pipeline" : "active"
      );
      syncCapturedIdentityControls();
      if (identity.state && state.status !== "streaming") {
        state.status = String(identity.state);
      }
      return identity;
    }

    function syncCapturedIdentityControls() {
      const contextInput = byId("context-id");
      const taskInput = byId("task-id");
      const activeTaskInput = byId("active-task-id");
      if (contextInput && state.contextId && contextInput.value.trim() !== state.contextId) {
        contextInput.value = state.contextId;
      }
      if (taskInput && state.taskId && taskInput.value.trim() !== state.taskId) {
        taskInput.value = state.taskId;
      }
      if (activeTaskInput && state.activeTaskId && activeTaskInput.value.trim() !== state.activeTaskId) {
        activeTaskInput.value = state.activeTaskId;
      }
      if (
        activeTaskInput &&
        !state.activeTaskId &&
        activeTaskInput.value.trim() &&
        (state.normalHandoffReady ||
          (isTerminalPipelineTaskState(state.status) && activeTaskInput.value.trim() === state.taskId))
      ) {
        activeTaskInput.value = "";
      }
    }

    function isNormalHandoffEnvelope(envelope) {
      const data = eventData(envelope);
      return (
        envelope &&
        eventTypeOf(envelope) === "pipeline_handoff_ready" &&
        data &&
        data.action === "switch_to_normal" &&
        (data.targetMode || data.target_mode) === "normal"
      );
    }

    function clearActiveTaskForNormalHandoff() {
      const activeTaskInput = byId("active-task-id");
      if (!state.activeTaskId || state.activeTaskId === state.taskId) {
        state.activeTaskId = "";
      }
      if (activeTaskInput && (!activeTaskInput.value.trim() || activeTaskInput.value.trim() === state.taskId)) {
        activeTaskInput.value = "";
      }
    }

    function updateNormalHandoffState(envelope) {
      if (!isNormalHandoffEnvelope(envelope)) {
        if (envelope && eventTypeOf(envelope) === "pipeline_started") {
          state.normalHandoffReady = false;
        }
        return;
      }
      state.normalHandoffReady = true;
      clearActiveTaskForNormalHandoff();
    }

    function streamTaskIdForControls(controls) {
      const activeTaskId = controls.activeTaskId || state.activeTaskId;
      const pipelineTaskId = controls.taskId || state.taskId;
      if (activeTaskId && !(isTerminalPipelineTaskState(state.status) && activeTaskId === pipelineTaskId)) {
        return activeTaskId;
      }
      if (state.normalHandoffReady || isTerminalPipelineTaskState(state.status)) {
        return "";
      }
      return pipelineTaskId;
    }

    function cancelTaskIdForControls(controls) {
      return controls.activeTaskId || state.activeTaskId || controls.taskId || state.taskId;
    }

    function nextBrowserPaint() {
      return new Promise((resolve) => {
        const scheduleFrame = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 16));
        scheduleFrame(() => resolve());
      });
    }

    async function yieldToBrowserAfterStreamEvent(payload, streamEventCount) {
      if (document.hidden) {
        return;
      }
      const envelope = extractPipelineEnvelope(payload);
      const eventType = envelope ? eventTypeOf(envelope) : "";
      if (eventType !== "text_delta" || streamEventCount % 8 === 0) {
        await nextBrowserPaint();
      }
    }

    async function cancelReaderSafely(reader) {
      try {
        await reader.cancel();
      } catch {
        // The reader may already be closed by the server.
      }
    }

    function shouldStopStreamingAfterPayload(payload) {
      const envelope = extractPipelineEnvelope(payload);
      if (envelope && eventTypeOf(envelope) === "input_required") {
        return true;
      }
      const identity = a2aTaskIdentity(payload);
      const stateValue = String((identity && identity.state) || "").toUpperCase();
      return stateValue === "TASK_STATE_INPUT_REQUIRED";
    }

    function rawItemValue(item) {
      if (item && typeof item === "object" && Object.prototype.hasOwnProperty.call(item, "value")) {
        return item.value;
      }
      return item;
    }

    function rawItemTimestamp(item) {
      return item && typeof item === "object" ? item.at || item.receivedAt || "" : "";
    }

    function pipelineEnvelopeFromRawItem(item) {
      return extractPipelineEnvelope(rawItemValue(item));
    }

    function a2aMessageText(message) {
      if (!message || typeof message !== "object" || !Array.isArray(message.parts)) {
        return "";
      }
      return message.parts
        .map((part) => part && typeof part === "object" && typeof part.text === "string" ? part.text : "")
        .join("");
    }

    function a2aStatusMessage(payload) {
      if (!payload || typeof payload !== "object") {
        return null;
      }
      if (Array.isArray(payload)) {
        for (const item of payload) {
          const message = a2aStatusMessage(item);
          if (message) {
            return message;
          }
        }
        return null;
      }
      if (payload.result) {
        return a2aStatusMessage(payload.result);
      }
      const statusUpdate = payload.statusUpdate || payload.status_update;
      if (!statusUpdate || typeof statusUpdate !== "object") {
        return null;
      }
      const status = statusUpdate.status && typeof statusUpdate.status === "object" ? statusUpdate.status : {};
      const message = status.message && typeof status.message === "object" ? status.message : null;
      const text = a2aMessageText(message);
      if (!text) {
        return null;
      }
      return {
        kind: "agent_message_delta",
        taskId: statusUpdate.taskId || statusUpdate.task_id || "",
        contextId: statusUpdate.contextId || statusUpdate.context_id || "",
        state: status.state || "",
        messageId: message.messageId || message.message_id || "",
        role: message.role || "ROLE_AGENT",
        text
      };
    }

    function eventTypeFromRawItem(item) {
      const value = rawItemValue(item);
      const envelope = pipelineEnvelopeFromRawItem(item);
      if (envelope && (envelope.eventType || envelope.event_type)) {
        return String(envelope.eventType || envelope.event_type);
      }
      if (a2aStatusMessage(value)) {
        return "agent_message_delta";
      }
      if (value && typeof value === "object") {
        if (value.type) {
          return String(value.type);
        }
        if (value.method) {
          return String(value.method);
        }
        if (value.error) {
          return "error";
        }
      }
      const identity = a2aTaskIdentity(value);
      if (identity && identity.kind === "task_submitted") {
        return "task_submitted";
      }
      if (identity && identity.kind === "status_update") {
        return "status_update";
      }
      return "event";
    }

    function textDeltaText(item) {
      const envelope = pipelineEnvelopeFromRawItem(item);
      const data = envelope && envelope.data && typeof envelope.data === "object" ? envelope.data : {};
      return typeof data.text === "string" ? data.text : "";
    }

    function textDeltaGroupKey(item) {
      const envelope = pipelineEnvelopeFromRawItem(item) || {};
      return [
        envelope.contextId || envelope.context_id || "",
        envelope.taskId || envelope.task_id || "",
        envelope.pipelineRunId || envelope.pipeline_run_id || "",
        envelope.scope || "pipeline",
        envelope.step && envelope.step.runId ? envelope.step.runId : "",
        envelope.candidate && envelope.candidate.runId ? envelope.candidate.runId : "",
        envelope.candidateStep && envelope.candidateStep.runId ? envelope.candidateStep.runId : ""
      ].join("|");
    }

    function normalMessageGroupKey(item) {
      const message = a2aStatusMessage(rawItemValue(item)) || {};
      return [
        message.contextId || "",
        message.taskId || "",
        message.messageId || "",
        message.role || "ROLE_AGENT"
      ].join("|");
    }

    function flushTextDeltaGroup(target, group) {
      if (group) {
        target.push(group);
      }
      return null;
    }

    function groupSseEvents(events) {
      const grouped = [];
      let currentGroup = null;
      events.forEach((event) => {
        const eventType = eventTypeFromRawItem(event);
        if (eventType === "text_delta") {
          const envelope = pipelineEnvelopeFromRawItem(event) || {};
          const key = textDeltaGroupKey(event);
          const text = textDeltaText(event);
          if (!currentGroup || currentGroup.key !== key) {
            currentGroup = flushTextDeltaGroup(grouped, currentGroup);
            currentGroup = {
              type: "text_delta_group",
              key,
              count: 0,
              text: "",
              events: [],
              firstAt: event.at,
              lastAt: event.at,
              firstSequence: envelope.sequence || "",
              lastSequence: envelope.sequence || "",
              contextId: envelope.contextId || envelope.context_id || "",
              taskId: envelope.taskId || envelope.task_id || ""
            };
          }
          currentGroup.count += 1;
          currentGroup.text += text;
          currentGroup.events.push(event);
          currentGroup.lastAt = event.at;
          currentGroup.lastSequence = envelope.sequence || currentGroup.lastSequence;
          return;
        }
        if (eventType === "agent_message_delta") {
          const message = a2aStatusMessage(rawItemValue(event)) || {};
          const key = normalMessageGroupKey(event);
          if (!currentGroup || currentGroup.key !== key || currentGroup.type !== "a2a_message_group") {
            currentGroup = flushTextDeltaGroup(grouped, currentGroup);
            currentGroup = {
              type: "a2a_message_group",
              key,
              count: 0,
              text: "",
              events: [],
              firstAt: rawItemTimestamp(event),
              lastAt: rawItemTimestamp(event),
              contextId: message.contextId || "",
              taskId: message.taskId || "",
              role: message.role || "ROLE_AGENT"
            };
          }
          currentGroup.count += 1;
          currentGroup.text += message.text || "";
          currentGroup.events.push(event);
          currentGroup.lastAt = rawItemTimestamp(event);
          return;
        }
        currentGroup = flushTextDeltaGroup(grouped, currentGroup);
        grouped.push({type: "event", event});
      });
      flushTextDeltaGroup(grouped, currentGroup);
      return grouped;
    }

    function permissionFromEnvelope(envelope) {
      if (!envelope || typeof envelope !== "object") {
        return null;
      }
      if (envelope.permission && typeof envelope.permission === "object") {
        return envelope.permission;
      }
      if (envelope.eventType === "permission_requested" && envelope.data && typeof envelope.data === "object") {
        return envelope.data;
      }
      return null;
    }

    function latestPermissionFromSnapshot(envelope) {
      const display = envelope && envelope.display && typeof envelope.display === "object" ? envelope.display : {};
      const permissions = Array.isArray(display.permissions) ? display.permissions : [];
      return permissions.length > 0 ? permissions[permissions.length - 1] : null;
    }

    function permissionGuidance(permission) {
      if (!permission || typeof permission !== "object") {
        return "";
      }
      const tool = permission.toolName || permission.toolUseId || permission.permissionId || "tool";
      const summary = permission.safeSummary || "permission request";
      let approved = null;
      if (typeof permission.approved === "boolean") {
        approved = permission.approved;
      } else if (permission.decision === "allow_once") {
        approved = true;
      } else if (permission.decision === "deny") {
        approved = false;
      }
      const decision = permission.decision || (approved === true ? "allow_once" : "pending");
      const lines = [
        `${tool}: ${summary}`,
        `decision: ${decision}`
      ];
      if (approved === false || decision === "deny") {
        lines.push(
          "Permission denied by A2A server. For manual end-to-end smoke tests, start iac-code a2a " +
            "with a config file containing: auto_approve_permissions: true"
        );
      } else if (approved === true) {
        lines.push("Permission approved by A2A server.");
      } else {
        lines.push("Permission requested. The debugger observes this event; approval is resolved by the A2A server.");
      }
      return lines.join("\\n");
    }

    function rawEventLabel(row) {
      if (row.type === "text_delta_group") {
        return `text_delta x${row.count}`;
      }
      if (row.type === "a2a_message_group") {
        return `agent message x${row.count}`;
      }
      const item = row.event || row;
      const envelope = pipelineEnvelopeFromRawItem(item);
      const eventType = eventTypeFromRawItem(item);
      const sequence = envelope && envelope.sequence ? `#${envelope.sequence} ` : "";
      return `${sequence}${eventType}`;
    }

    function rawEventSummary(row) {
      if (row.type === "text_delta_group") {
        return row.text || "(empty text delta)";
      }
      if (row.type === "a2a_message_group") {
        return row.text || "(empty agent message)";
      }
      const item = row.event || row;
      const value = rawItemValue(item);
      const envelope = pipelineEnvelopeFromRawItem(item);
      const permission = permissionFromEnvelope(envelope);
      if (permission) {
        return permission.safeSummary || `${permission.toolName || permission.toolUseId || "tool"} permission`;
      }
      if (envelope && envelope.data && typeof envelope.data === "object") {
        return envelope.data.summary || envelope.data.message || envelope.data.text || envelope.status || "";
      }
      if (value && typeof value === "object") {
        const identity = a2aTaskIdentity(value);
        if (identity) {
          return [identity.state, identity.taskId].filter(Boolean).join(" · ");
        }
        if (value.type === "health" && value.body && typeof value.body === "object") {
          const health =
            value.body.health && value.body.health.status ? `health: ${value.body.health.status}` : "health checked";
          const agent =
            value.body.agentCard && value.body.agentCard.name ? `agent: ${value.body.agentCard.name}` : "";
          return [health, agent].filter(Boolean).join(", ");
        }
        if (value.type === "cancel") {
          return value.body && value.body.ok === false ? "cancel failed" : "cancel response";
        }
        if (value.type === "error") {
          return value.error ? String(value.error) : "debugger error";
        }
        if (value.error) {
          return String(value.error);
        }
        if (value.body && typeof value.body === "object") {
          return value.body.ok === false ? asPrettyJson(value.body.error || value.body) : asPrettyJson(value.body);
        }
      }
      return typeof value === "string" ? value : "";
    }

    function rawEventMeta(row) {
      if (row.type === "text_delta_group") {
        const range =
          row.firstSequence && row.lastSequence && row.firstSequence !== row.lastSequence
            ? `seq ${row.firstSequence}-${row.lastSequence}`
            : row.firstSequence
              ? `seq ${row.firstSequence}`
              : "";
        return [range, row.lastAt].filter(Boolean).join(" · ");
      }
      if (row.type === "a2a_message_group") {
        return [row.role || "ROLE_AGENT", row.taskId || "", row.lastAt || ""].filter(Boolean).join(" · ");
      }
      const item = row.event || row;
      const envelope = pipelineEnvelopeFromRawItem(item);
      const scope = envelope && envelope.scope ? envelope.scope : "";
      return [scope, rawItemTimestamp(item)].filter(Boolean).join(" · ");
    }

    function statusFromEventType(eventType, fallback, scope, envelope) {
      if (eventType === "input_received" && scope === "step") {
        return eventData(envelope).kind === "ask_user_question" ? "working" : "completed";
      }
      const statusesByScope = {
        step: {
          "step_started": "working",
          "step_completed": "completed",
          "step_failed": "failed",
          "input_required": "waiting_input"
        },
        candidate: {
          "candidate_started": "working",
          "candidate_completed": "completed",
          "candidate_failed": "failed",
          "candidate_restart_requested": "restarting",
          "candidate_selected": "selected"
        },
        candidateStep: {
          "candidate_step_started": "working",
          "candidate_step_completed": "completed",
          "candidate_step_failed": "failed"
        }
      };
      const statuses = statusesByScope[scope] || {};
      return statuses[eventType] || fallback || "observed";
    }

    function waitingInputFromEnvelope(envelope, fallback) {
      if (!envelope || typeof envelope !== "object") {
        return fallback || "";
      }
      if (
        envelope.eventType === "input_required" &&
        envelope.data &&
        typeof envelope.data === "object" &&
        (envelope.data.prompt || envelope.data.options || envelope.data.stepId || envelope.data.step_id)
      ) {
        return envelope.data;
      }
      return (
        envelope.waitingInput ||
        envelope.waiting_input ||
        envelope.inputRequest ||
        envelope.pendingInput ||
        envelope.pending_input ||
        envelope.input ||
        fallback ||
        ""
      );
    }

    function cssEscape(value) {
      if (window.CSS && typeof window.CSS.escape === "function") {
        return window.CSS.escape(String(value));
      }
      return String(value).replace(/"/g, "");
    }

    function eventTypeOf(envelope) {
      return String((envelope && (envelope.eventType || envelope.event_type)) || "event");
    }

    function eventData(envelope) {
      return envelope && envelope.data && typeof envelope.data === "object" ? envelope.data : {};
    }

    function treeId(prefix, value) {
      return `${prefix}:${String(value || "unknown")}`;
    }

    function coordinateRunId(prefix, coordinate, fallback) {
      const value =
        coordinate && typeof coordinate === "object"
          ? coordinate.runId || coordinate.run_id || coordinate.id || coordinate.name
          : "";
      return treeId(prefix, value || fallback);
    }

    function pipelineTreeKey(envelope) {
      return treeId(
        "pipeline",
        envelope.pipelineRunId || envelope.pipeline_run_id || envelope.contextId || envelope.context_id || "current"
      );
    }

    function treeNodeMeta(envelope, coordinate) {
      const items = [];
      if (coordinate && coordinate.id) {
        items.push(`id: ${coordinate.id}`);
      }
      if (coordinate && coordinate.runId) {
        items.push(`run: ${coordinate.runId}`);
      }
      if (envelope.sequence) {
        items.push(`seq ${envelope.sequence}`);
      }
      return items.join(" · ");
    }

    function numericTreeValue(value) {
      if (value === undefined || value === null || value === "") {
        return null;
      }
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }

    function treeCoordinateOrder(coordinate) {
      if (!coordinate || typeof coordinate !== "object") {
        return null;
      }
      for (const field of ["index", "order", "stepIndex", "candidateIndex", "candidate_index"]) {
        const value = numericTreeValue(coordinate[field]);
        if (value !== null) {
          return value;
        }
      }
      return null;
    }

    function treeSequenceOrder(envelope) {
      return numericTreeValue(envelope && (envelope.sequence || envelope.seq));
    }

    function treeNodeOrder(key) {
      const node = state.executionTree.nodes[key];
      if (!node) {
        return Number.MAX_SAFE_INTEGER;
      }
      const explicit = numericTreeValue(node.sortOrder);
      if (explicit !== null) {
        return explicit;
      }
      const firstSequence = numericTreeValue(node.firstSequence);
      return firstSequence !== null ? 1000000 + firstSequence : Number.MAX_SAFE_INTEGER;
    }

    function sortedTreeChildKeys(keys) {
      return [...keys].sort((left, right) => {
        const orderDiff = treeNodeOrder(left) - treeNodeOrder(right);
        if (orderDiff !== 0) {
          return orderDiff;
        }
        const leftNode = state.executionTree.nodes[left] || {};
        const rightNode = state.executionTree.nodes[right] || {};
        return String(leftNode.label || left).localeCompare(String(rightNode.label || right));
      });
    }

    function ensureTreeNode(key, parentKey, attrs) {
      let node = state.executionTree.nodes[key];
      if (!node) {
        node = {
          key,
          parentKey: parentKey || "",
          kind: attrs.kind || "event",
          label: attrs.label || key,
          status: attrs.status || "observed",
          meta: attrs.meta || "",
          sortOrder: numericTreeValue(attrs.sortOrder),
          firstSequence: numericTreeValue(attrs.firstSequence),
          children: [],
          timeline: []
        };
        state.executionTree.nodes[key] = node;
        if (parentKey) {
          const parent = state.executionTree.nodes[parentKey];
          if (parent && !parent.children.includes(key)) {
            parent.children.push(key);
            state.executionTree.touchedKeys.add(parentKey);
          }
        } else if (!state.executionTree.rootIds.includes(key)) {
          state.executionTree.rootIds.push(key);
        }
        if (node.kind === "pipeline" || node.kind === "step" || node.kind === "parallel") {
          state.expandedTreeKeys.add(key);
        }
      }

      const previousSortOrder = node.sortOrder;
      const previousFirstSequence = node.firstSequence;
      Object.assign(node, {...attrs, key, parentKey: parentKey || node.parentKey || ""});
      const sortOrder = numericTreeValue(attrs.sortOrder);
      if (sortOrder !== null) {
        node.sortOrder = sortOrder;
      } else {
        node.sortOrder = previousSortOrder;
      }
      const firstSequence = numericTreeValue(attrs.firstSequence);
      if (firstSequence !== null) {
        const currentFirstSequence = numericTreeValue(node.firstSequence);
        node.firstSequence =
          currentFirstSequence === null ? firstSequence : Math.min(currentFirstSequence, firstSequence);
      } else {
        node.firstSequence = previousFirstSequence;
      }
      state.executionTree.touchedKeys.add(key);
      return node;
    }

    function ensurePipelineNode(envelope) {
      const key = pipelineTreeKey(envelope);
      return ensureTreeNode(key, "", {
        kind: "pipeline",
        label: envelope.pipelineName || envelope.pipeline_name || "Pipeline",
        status: envelope.status || state.status || "working",
        firstSequence: treeSequenceOrder(envelope),
        meta: [
          envelope.taskId || envelope.task_id,
          envelope.contextId || envelope.context_id
        ].filter(Boolean).join(" · ")
      });
    }

    function ensureNormalChatNode(message) {
      const key = treeId("normal-chat", message.contextId || "current");
      return ensureTreeNode(key, "", {
        kind: "normal-chat",
        label: "Normal Chat",
        status: message.state || "working",
        meta: [
          message.taskId ? `task: ${message.taskId}` : "",
          message.contextId ? `context: ${message.contextId}` : ""
        ].filter(Boolean).join(" · ")
      });
    }

    function stepLabel(step) {
      const index = step && step.index ? `${step.index}${step.total ? `/${step.total}` : ""}: ` : "";
      return `Step ${index}${(step && (step.name || step.id)) || "step"}`;
    }

    function ensureStepNode(envelope, pipelineKey) {
      if (!envelope.step || typeof envelope.step !== "object") {
        return null;
      }
      const key = coordinateRunId("step", envelope.step, `step-${envelope.sequence || "unknown"}`);
      const node = ensureTreeNode(key, pipelineKey, {
        kind: "step",
        label: stepLabel(envelope.step),
        status: statusFromEventType(eventTypeOf(envelope), envelope.step.status, "step", envelope),
        sortOrder: treeCoordinateOrder(envelope.step),
        firstSequence: treeSequenceOrder(envelope),
        meta: treeNodeMeta(envelope, envelope.step)
      });
      const eventType = eventTypeOf(envelope);
      if (
        state.executionTree.lastStepKey !== key ||
        ["step_started", "step_completed", "step_failed"].includes(eventType)
      ) {
        state.executionTree.lastCandidateKey = "";
        state.executionTree.lastCandidateStepKey = "";
      }
      state.executionTree.lastStepKey = key;
      return node;
    }

    function ensureParallelGroupNode(stepNode) {
      if (!stepNode) {
        return null;
      }
      return ensureTreeNode(treeId("parallel", stepNode.key), stepNode.key, {
        kind: "parallel",
        label: "Parallel Candidates",
        status: stepNode.status || "working",
        sortOrder: treeNodeOrder(stepNode.key),
        firstSequence: numericTreeValue(stepNode.firstSequence),
        meta: "subpipelines run concurrently"
      });
    }

    function candidateLabel(candidate) {
      const index = candidate && Number.isInteger(candidate.index) ? ` #${candidate.index + 1}` : "";
      const name = candidate && (candidate.name || candidate.pipelineName || candidate.id);
      return `Candidate${index}: ${name || "candidate"}`;
    }

    function ensureCandidateNode(envelope, parentKey) {
      if (!envelope.candidate || typeof envelope.candidate !== "object" || !parentKey) {
        return null;
      }
      const key = coordinateRunId("candidate", envelope.candidate, `candidate-${envelope.sequence || "unknown"}`);
      const node = ensureTreeNode(key, parentKey, {
        kind: "candidate",
        label: candidateLabel(envelope.candidate),
        status: statusFromEventType(eventTypeOf(envelope), envelope.candidate.status, "candidate", envelope),
        sortOrder: treeCoordinateOrder(envelope.candidate),
        firstSequence: treeSequenceOrder(envelope),
        meta: treeNodeMeta(envelope, envelope.candidate)
      });
      if (state.executionTree.lastCandidateKey !== key) {
        state.executionTree.lastCandidateStepKey = "";
      }
      state.executionTree.lastCandidateKey = key;
      return node;
    }

    function candidateStepLabel(candidateStep) {
      const index =
        candidateStep && candidateStep.index
          ? `${candidateStep.index}${candidateStep.total ? `/${candidateStep.total}` : ""}: `
          : "";
      return `Candidate Step ${index}${(candidateStep && (candidateStep.name || candidateStep.id)) || "step"}`;
    }

    function ensureCandidateStepNode(envelope, candidateNode) {
      if (!envelope.candidateStep || typeof envelope.candidateStep !== "object" || !candidateNode) {
        return null;
      }
      const key = coordinateRunId(
        "candidate-step",
        envelope.candidateStep,
        `candidate-step-${envelope.sequence || "unknown"}`
      );
      const node = ensureTreeNode(key, candidateNode.key, {
        kind: "candidate-step",
        label: candidateStepLabel(envelope.candidateStep),
        status: statusFromEventType(
          eventTypeOf(envelope),
          envelope.candidateStep.status,
          "candidateStep",
          envelope
        ),
        sortOrder: treeCoordinateOrder(envelope.candidateStep),
        firstSequence: treeSequenceOrder(envelope),
        meta: treeNodeMeta(envelope, envelope.candidateStep)
      });
      state.executionTree.lastCandidateStepKey = key;
      return node;
    }

    function ensureCandidateSkeletonSteps(candidateNode, candidate) {
      if (!candidateNode || !candidate || !Array.isArray(candidate.steps)) {
        return;
      }
      candidate.steps.forEach((step) => {
        const key = coordinateRunId("candidate-step", step, `${candidateNode.key}-${step.id || "step"}`);
        ensureTreeNode(key, candidateNode.key, {
          kind: "candidate-step",
          label: candidateStepLabel(step),
          status: step.status || "pending",
          sortOrder: treeCoordinateOrder(step),
          meta: treeNodeMeta({}, step)
        });
      });
    }

    function timelineTargetKeyFromEnvelope(envelope, nodes) {
      const type = eventTypeOf(envelope);
      const pipeline = nodes.pipeline;
      const stepKey = nodes.step ? nodes.step.key : state.executionTree.lastStepKey;
      const candidateKey = nodes.candidate ? nodes.candidate.key : state.executionTree.lastCandidateKey;
      const candidateStepKey = nodes.candidateStep
        ? nodes.candidateStep.key
        : state.executionTree.lastCandidateStepKey;
      if (type.startsWith("interrupt_") || envelope.scope === "interrupt") {
        return stepKey || pipeline.key;
      }
      if (["text_delta", "tool_result", "permission_requested", "input_required"].includes(type)) {
        return candidateStepKey || candidateKey || stepKey || pipeline.key;
      }
      if (type.startsWith("candidate_step_")) {
        return candidateStepKey || candidateKey || stepKey || pipeline.key;
      }
      if (type.startsWith("candidate_")) {
        return candidateKey || stepKey || pipeline.key;
      }
      if (type.startsWith("pipeline_")) {
        return pipeline.key;
      }
      if (type.startsWith("step_") || type === "input_received") {
        return stepKey || pipeline.key;
      }
      return candidateStepKey || candidateKey || stepKey || pipeline.key;
    }

    function summarizeValue(value, maxLength = 500) {
      if (value === undefined || value === null || value === "") {
        return "";
      }
      return compactText(asPrettyJson(value), maxLength);
    }

    function summarizeTimelineEvent(envelope) {
      const type = eventTypeOf(envelope);
      const data = eventData(envelope);
      if (type === "text_delta") {
        return {label: "text", text: data.text || "", className: "timeline-text"};
      }
      if (type === "tool_result") {
        return {
          label: `tool: ${data.toolName || data.tool_name || "tool"}`,
          text: summarizeValue(data.result || data.output || data),
          className: "timeline-tool"
        };
      }
      if (type === "permission_requested") {
        const permission = permissionFromEnvelope(envelope) || {};
        return {
          label: `permission: ${permission.toolName || data.toolName || "tool"}`,
          text: permission.safeSummary || summarizeValue(data),
          className: "timeline-permission"
        };
      }
      if (type === "rollback_completed" || type === "rollback_triggered") {
        const fromStep = data.fromStep || data.from_step || envelope.step && envelope.step.id || "";
        const toStep = data.toStep || data.to_step || data.rollbackTarget || "";
        const reason = data.reason ? ` (${data.reason})` : "";
        return {
          label: "rollback",
          text: [fromStep, toStep].filter(Boolean).join(" -> ") + reason,
          className: "timeline-rollback"
        };
      }
      if (type === "input_required") {
        return {label: "input required", text: summarizeValue(data), className: "timeline-permission"};
      }
      if (type === "pipeline_canceled") {
        return {
          label: "pipeline canceled",
          text: data.reason || summarizeValue(data),
          className: "timeline-canceled"
        };
      }
      if (type.endsWith("_completed") && Object.prototype.hasOwnProperty.call(data, "conclusion")) {
        return {
          label: type.replace(/_/g, " "),
          text: summarizeValue(data.conclusion),
          className: "timeline-event"
        };
      }
      return {label: type.replace(/_/g, " "), text: summarizeValue(data), className: "timeline-event"};
    }

    function appendTimelineItem(targetKey, envelope) {
      const node = state.executionTree.nodes[targetKey];
      if (!node) {
        return;
      }
      const type = eventTypeOf(envelope);
      const summary = summarizeTimelineEvent(envelope);
      const textKey = `text:${targetKey}`;
      if (type === "text_delta") {
        let item = state.executionTree.textGroups[textKey];
        if (!item) {
          item = {
            key: `${textKey}:${envelope.sequence || node.timeline.length}`,
            type,
            label: "text",
            text: "",
            meta: "",
            className: "timeline-text",
            count: 0,
            raw: {
              eventType: "text_delta_group",
              targetKey,
              text: "",
              events: []
            }
          };
          state.executionTree.textGroups[textKey] = item;
          node.timeline.push(item);
        }
        item.count += 1;
        item.text += summary.text || "";
        item.meta = `text_delta x${item.count}`;
        item.raw.count = item.count;
        item.raw.text = item.text;
        item.raw.events.push(envelope);
      } else {
        delete state.executionTree.textGroups[textKey];
        node.timeline.push({
          key: envelope.eventId || envelope.event_id || `${type}:${envelope.sequence || node.timeline.length}`,
          type,
          label: summary.label,
          text: summary.text,
          meta: [envelope.sequence ? `seq ${envelope.sequence}` : "", envelope.createdAt || envelope.created_at || ""]
            .filter(Boolean)
            .join(" · "),
          className: summary.className,
          raw: envelope
        });
      }
      state.executionTree.touchedKeys.add(targetKey);
    }

    function appendNormalMessageEvent(row) {
      const message = a2aStatusMessage(rawItemValue(row));
      if (!message) {
        return;
      }
      const node = ensureNormalChatNode(message);
      const groupKey = `normal:${node.key}:${normalMessageGroupKey(row)}`;
      let item = state.executionTree.normalMessageGroups[groupKey];
      if (!item) {
        item = {
          key: groupKey,
          type: "agent_message_delta",
          label: "agent message",
          text: "",
          meta: "",
          className: "timeline-text",
          count: 0,
          raw: {
            eventType: "a2a_message_group",
            targetKey: node.key,
            text: "",
            events: []
          }
        };
        state.executionTree.normalMessageGroups[groupKey] = item;
        node.timeline.push(item);
      }
      item.count += 1;
      item.text += message.text || "";
      item.meta = `agent_message_delta x${item.count}`;
      item.raw.count = item.count;
      item.raw.text = item.text;
      item.raw.events.push(rawItemValue(row));
      state.executionTree.touchedKeys.add(node.key);
    }

    function appendExecutionTreeEvent(envelope) {
      if (!envelope || typeof envelope !== "object") {
        return;
      }
      const pipeline = ensurePipelineNode(envelope);
      const step = ensureStepNode(envelope, pipeline.key);
      const needsParallel =
        envelope.candidate && typeof envelope.candidate === "object" ||
        envelope.candidateStep && typeof envelope.candidateStep === "object";
      const parallel = needsParallel ? ensureParallelGroupNode(step || pipeline) : null;
      const candidate = ensureCandidateNode(envelope, parallel ? parallel.key : step ? step.key : pipeline.key);
      ensureCandidateSkeletonSteps(candidate, envelope.candidate);
      const candidateStep = ensureCandidateStepNode(envelope, candidate);
      const targetKey = timelineTargetKeyFromEnvelope(envelope, {pipeline, step, candidate, candidateStep});
      appendTimelineItem(targetKey, envelope);
    }

    function candidateStepFromSnapshot(candidateStep) {
      const id = String(candidateStep.runId || candidateStep.id || candidateStep.name || "candidate-step");
      return {
        id,
        name: String(candidateStep.name || candidateStep.title || candidateStep.id || id),
        status: String(candidateStep.status || candidateStep.state || candidateStep.phase || "observed"),
        raw: candidateStep
      };
    }

    function candidateSummary(candidate) {
      const rawSteps = Array.isArray(candidate.steps) ? candidate.steps : [];
      const steps = rawSteps.map((candidateStep) => {
        const normalized = candidateStepFromSnapshot(candidateStep);
        return `${normalized.name}: ${normalized.status}`;
      });
      const title = candidate.name || candidate.pipelineName || candidate.id || candidate.runId || "candidate";
      const status = candidate.status || candidate.state || "observed";
      return steps.length > 0 ? `${title}: ${status}\\n  ${steps.join("\\n  ")}` : `${title}: ${status}`;
    }

    function candidateFromSnapshot(candidate) {
      const id = String(candidate.runId || candidate.id || candidate.name || "candidate");
      const steps = Array.isArray(candidate.steps) ? candidate.steps.map(candidateStepFromSnapshot) : [];
      return {
        id,
        name: String(candidate.name || candidate.pipelineName || candidate.id || id),
        status: String(candidate.status || candidate.state || "observed"),
        summary: candidateSummary({...candidate, steps}),
        steps,
        raw: candidate
      };
    }

    function stageFromStep(step) {
      const id = String(step.runId || step.id || step.stepId || step.name || "step");
      const candidates = Array.isArray(step.candidates) ? step.candidates.map(candidateFromSnapshot) : [];
      const candidateText = candidates.map(candidateSummary).join("\\n");
      return {
        id,
        name: String(step.name || step.title || step.id || id),
        status: String(step.status || step.state || step.phase || "observed"),
        summary: step.summary || step.message || step.text || step.detail || candidateText,
        candidates,
        raw: step
      };
    }

    function normalizeStage(rawStage) {
      if (Array.isArray(rawStage.candidates) || rawStage.runId) {
        return stageFromStep(rawStage);
      }
      const id = String(rawStage.id || rawStage.stageId || rawStage.name || rawStage.type || "stage");
      return {
        id,
        name: String(rawStage.name || rawStage.title || id),
        status: String(rawStage.status || rawStage.state || rawStage.phase || "observed"),
        summary: rawStage.summary || rawStage.message || rawStage.text || rawStage.detail || "",
        candidates: [],
        raw: rawStage
      };
    }

    function upsertStage(stage) {
      const nextStage = normalizeStage(stage);
      const index = state.stages.findIndex((item) => item.id === nextStage.id);
      if (index >= 0) {
        const candidates = nextStage.candidates.length > 0 ? nextStage.candidates : state.stages[index].candidates;
        if (nextStage.status === "observed" && state.stages[index].status) {
          nextStage.status = state.stages[index].status;
        }
        state.stages[index] = {...state.stages[index], ...nextStage, candidates};
        return state.stages[index];
      }
      state.stages.push(nextStage);
      return nextStage;
    }

    function upsertCandidate(stepId, candidate, candidateStep) {
      const normalizedStepId = String(stepId || (candidate && candidate.parentStepRunId) || "step");
      let stage = state.stages.find((item) => item.id === normalizedStepId);
      if (!stage) {
        stage = upsertStage({
          runId: normalizedStepId,
          id: normalizedStepId,
          status: "working",
          candidates: []
        });
      }

      const nextCandidate = candidateFromSnapshot(candidate || {runId: "candidate", id: "candidate"});
      const candidateIndex = stage.candidates.findIndex((item) => item.id === nextCandidate.id);
      const currentCandidate = candidateIndex >= 0 ? stage.candidates[candidateIndex] : null;
      const currentSteps = currentCandidate ? currentCandidate.steps : [];
      nextCandidate.steps = nextCandidate.steps.length > 0 ? nextCandidate.steps : currentSteps;
      if (currentCandidate && nextCandidate.status === "observed" && currentCandidate.status) {
        nextCandidate.status = currentCandidate.status;
      }

      if (candidateStep && typeof candidateStep === "object") {
        const nextStep = candidateStepFromSnapshot(candidateStep);
        const candidateStepIndex = nextCandidate.steps.findIndex((item) => item.id === nextStep.id);
        if (candidateStepIndex >= 0) {
          if (nextStep.status === "observed" && nextCandidate.steps[candidateStepIndex].status) {
            nextStep.status = nextCandidate.steps[candidateStepIndex].status;
          }
          nextCandidate.steps[candidateStepIndex] = {...nextCandidate.steps[candidateStepIndex], ...nextStep};
        } else {
          nextCandidate.steps.push(nextStep);
        }
      }

      nextCandidate.summary = candidateSummary(nextCandidate);
      if (candidateIndex >= 0) {
        stage.candidates[candidateIndex] = {...stage.candidates[candidateIndex], ...nextCandidate};
      } else {
        stage.candidates.push(nextCandidate);
      }
      stage.summary = stage.candidates.map(candidateSummary).join("\\n");
      return stage.candidates.find((item) => item.id === nextCandidate.id);
    }

    function applyPipelineEvent(payload, rawRow = null, options = {}) {
      captureA2ATaskIdentity(payload);
      const envelope = extractPipelineEnvelope(payload);
      if (!envelope || typeof envelope !== "object") {
        appendNormalMessageEvent(rawRow || payload);
        renderPipeline();
        return;
      }
      if (!options.alreadyRecorded && !rememberPipelineEvent(payload)) {
        return;
      }

      state.status = String(envelope.status || envelope.state || envelope.pipelineStatus || state.status || "running");
      state.taskId = String(envelope.taskId || envelope.task_id || state.taskId || "");
      state.contextId = String(envelope.contextId || envelope.context_id || state.contextId || "");
      if (state.taskId) {
        if (!state.normalHandoffReady && !state.activeTaskId) {
          state.activeTaskId = state.taskId;
        }
        recordTaskIdentity(
          {taskId: state.taskId, contextId: state.contextId, state: state.status},
          "pipeline"
        );
      }
      updateNormalHandoffState(envelope);
      syncCapturedIdentityControls();
      state.lastSequence = Number(
        envelope.sequence || envelope.lastSequence || envelope.seq || state.lastSequence || 0
      );
      state.recovery = String(
        envelope.recovery || envelope.recoveryStatus || envelope.resumeStatus || state.recovery || ""
      );
      if (envelope.eventType === "input_received") {
        state.waitingInput = "";
        state.latestPermission = null;
      } else {
        state.waitingInput = waitingInputFromEnvelope(envelope, state.waitingInput);
        if (envelope.eventType === "input_required") {
          state.latestPermission = null;
        }
      }
      const permission = permissionFromEnvelope(envelope);
      if (permission) {
        state.latestPermission = permission;
      }

      if (Array.isArray(envelope.stages)) {
        state.stages = envelope.stages.map(normalizeStage);
      } else if (Array.isArray(envelope.steps)) {
        state.stages = envelope.steps.map(stageFromStep);
      }

      if (envelope.stage && typeof envelope.stage === "object") {
        upsertStage(envelope.stage);
      } else if (envelope.stageId || envelope.stage_id || envelope.stageName) {
        upsertStage({
          id: envelope.stageId || envelope.stage_id || envelope.stageName,
          name: envelope.stageName || envelope.stageId || envelope.stage_id,
          status: envelope.stageStatus || envelope.status,
          summary: envelope.message || envelope.summary || envelope.text || "",
          raw: envelope
        });
      }

      let coordinateStepId = "";
      if (envelope.step && typeof envelope.step === "object") {
        const stepStatus = statusFromEventType(envelope.eventType, envelope.step.status, "step", envelope);
        const step = stepStatus ? {...envelope.step, status: stepStatus} : envelope.step;
        const stage = upsertStage(stageFromStep(step));
        coordinateStepId = stage.id;
      }
      if (envelope.candidate && typeof envelope.candidate === "object") {
        const candidateStatus = statusFromEventType(
          envelope.eventType,
          envelope.candidate.status,
          "candidate",
          envelope
        );
        const candidate = candidateStatus ? {...envelope.candidate, status: candidateStatus} : envelope.candidate;
        upsertCandidate(coordinateStepId, candidate, null);
      }
      if (envelope.candidateStep && typeof envelope.candidateStep === "object") {
        const candidateStatus = statusFromEventType(
          envelope.eventType,
          envelope.candidate && envelope.candidate.status,
          "candidate",
          envelope
        );
        const candidateStepStatus = statusFromEventType(
          envelope.eventType,
          envelope.candidateStep.status,
          "candidateStep",
          envelope
        );
        const candidate = candidateStatus
          ? {...(envelope.candidate || {}), status: candidateStatus}
          : envelope.candidate;
        const candidateStep = candidateStepStatus
          ? {...envelope.candidateStep, status: candidateStepStatus}
          : envelope.candidateStep;
        upsertCandidate(
          coordinateStepId,
          candidate,
          candidateStep
        );
      }

      appendExecutionTreeEvent(envelope);
      renderPipeline();
    }

    function rebuildFromSnapshot(snapshot) {
      const envelope = snapshot && snapshot.snapshot ? snapshot.snapshot : snapshot;
      state.raw.snapshot = snapshot;
      state.status = String((envelope && (envelope.status || envelope.state)) || "snapshot");
      state.taskId = String((envelope && (envelope.taskId || envelope.task_id)) || state.taskId || "");
      state.contextId = String((envelope && (envelope.contextId || envelope.context_id)) || state.contextId || "");
      state.normalHandoffReady = Boolean(normalHandoffSummary(envelope));
      if (state.taskId) {
        recordTaskIdentity(
          {taskId: state.taskId, contextId: state.contextId, state: state.status},
          "pipeline"
        );
        if (state.normalHandoffReady) {
          state.activeTaskId = "";
        } else if (!state.activeTaskId) {
          state.activeTaskId = state.taskId;
        }
      }
      syncCapturedIdentityControls();
      state.lastSequence = Number((envelope && (envelope.lastSequence || envelope.sequence)) || 0);
      state.recovery = String((envelope && (envelope.recovery || envelope.recoveryStatus)) || "");
      state.waitingInput = waitingInputFromEnvelope(envelope, "");
      if (state.normalHandoffReady) {
        clearActiveTaskForNormalHandoff();
      }
      if (state.waitingInput) {
        state.latestPermission = null;
      }
      state.latestPermission = latestPermissionFromSnapshot(envelope) || state.latestPermission;
      if (Array.isArray(envelope && envelope.steps)) {
        state.stages = envelope.steps.map(stageFromStep);
      } else {
        state.stages = Array.isArray(envelope && envelope.stages) ? envelope.stages.map(normalizeStage) : [];
      }
      renderPipeline();
      renderRaw();
    }

    function onTreeToggle(event) {
      event.stopPropagation();
      if (event.target !== event.currentTarget) {
        return;
      }
      const details = event.currentTarget;
      const key = details.getAttribute("data-tree-key");
      if (!key) {
        return;
      }
      if (details.open) {
        state.expandedTreeKeys.add(key);
      } else {
        state.expandedTreeKeys.delete(key);
      }
    }

    function ensureTreeToggleListener(details) {
      if (!details || details.getAttribute("data-tree-toggle-bound") === "true") {
        return;
      }
      details.addEventListener("toggle", onTreeToggle);
      details.setAttribute("data-tree-toggle-bound", "true");
    }

    function ensureTreeElement(node, parentElement) {
      let details = document.querySelector(`[data-tree-key="${cssEscape(node.key)}"]`);
      if (!details) {
        details = document.createElement("details");
        details.setAttribute("data-tree-key", node.key);

        const summary = document.createElement("summary");
        summary.className = "tree-summary";
        details.appendChild(summary);

        const body = document.createElement("div");
        body.className = "tree-node-body";
        const timeline = document.createElement("div");
        timeline.className = "tree-node-timeline";
        const children = document.createElement("div");
        children.className = "tree-node-children";
        body.appendChild(timeline);
        body.appendChild(children);
        details.appendChild(body);
      }
      ensureTreeToggleListener(details);
      details.className = ["tree-node", `tree-node-${node.kind}`].join(" ");
      if (node.kind === "parallel") {
        details.className += " parallel-group";
      }
      if (node.kind === "candidate") {
        details.className += " candidate-lane";
      }
      if (!details.parentElement || details.parentElement !== parentElement) {
        parentElement.appendChild(details);
      }
      details.open = state.expandedTreeKeys.has(node.key);
      return details;
    }

    function renderTreeSummary(details, node) {
      const summary = details.querySelector(":scope > .tree-summary");
      summary.textContent = "";
      const title = document.createElement("span");
      title.className = "tree-title";
      title.textContent = node.label;
      const meta = document.createElement("span");
      meta.className = "tree-meta";
      meta.textContent = node.meta || "";
      const status = document.createElement("span");
      status.className = "pill";
      status.textContent = node.status || "observed";
      summary.appendChild(title);
      summary.appendChild(meta);
      summary.appendChild(status);
    }

    function detailTextForTimelineItem(item) {
      if (item.raw !== undefined && item.raw !== null) {
        if (typeof item.raw === "string") {
          return item.raw;
        }
        try {
          return JSON.stringify(item.raw, null, 2);
        } catch {
          return String(item.raw);
        }
      }
      return item.text || "";
    }

    function renderTimelineDetails(item) {
      const detail = document.createElement("div");
      detail.className = "timeline-detail-body";
      detail.addEventListener("click", stopNestedTimelineToggle);
      detail.addEventListener("toggle", stopNestedTimelineToggle);
      if (state.expandedTimelineKeys.has(item.key)) {
        detail.classList.add("is-open");
      }
      const pre = document.createElement("pre");
      pre.textContent = detailTextForTimelineItem(item);
      detail.appendChild(pre);
      return detail;
    }

    function stopNestedTimelineToggle(event) {
      event.stopPropagation();
    }

    function renderTimelineItem(item) {
      const element = document.createElement("div");
      element.className = `timeline-item ${item.className || ""}`.trim();
      const title = document.createElement("div");
      title.className = "timeline-item-title";
      const label = document.createElement("span");
      label.textContent = item.label || item.type || "event";
      const actions = document.createElement("span");
      actions.className = "timeline-item-actions";
      const meta = document.createElement("span");
      meta.className = "timeline-item-meta";
      meta.textContent = item.meta || "";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "timeline-details-button";
      button.textContent = state.expandedTimelineKeys.has(item.key) ? "Hide" : "Details";
      button.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (state.expandedTimelineKeys.has(item.key)) {
          state.expandedTimelineKeys.delete(item.key);
        } else {
          state.expandedTimelineKeys.add(item.key);
        }
        renderPipeline();
      });
      title.appendChild(label);
      actions.appendChild(meta);
      actions.appendChild(button);
      title.appendChild(actions);
      element.appendChild(title);
      if (item.text) {
        const text = document.createElement("div");
        text.className = "timeline-item-text";
        text.textContent = item.text;
        element.appendChild(text);
      }
      element.appendChild(renderTimelineDetails(item));
      return element;
    }

    function renderTreeTimeline(details, node) {
      const timeline = details.querySelector(":scope > .tree-node-body > .tree-node-timeline");
      timeline.textContent = "";
      node.timeline.forEach((item) => {
        timeline.appendChild(renderTimelineItem(item));
      });
    }

    function renderTreeNode(treeKey, parentElement) {
      const node = state.executionTree.nodes[treeKey];
      if (!node) {
        return;
      }
      const details = ensureTreeElement(node, parentElement);
      renderTreeSummary(details, node);
      renderTreeTimeline(details, node);

      const children = details.querySelector(":scope > .tree-node-body > .tree-node-children");
      children.className = node.kind === "parallel" ? "tree-node-children candidate-lanes" : "tree-node-children";
      sortedTreeChildKeys(node.children).forEach((childKey) => renderTreeNode(childKey, children));
    }

    function renderExecutionTreePatch() {
      const container = byId("structured-pipeline");
      container.className = "execution-tree";
      const empty = container.querySelector(":scope > .empty");
      if (state.executionTree.rootIds.length === 0) {
        if (!empty) {
          const emptyNode = document.createElement("div");
          emptyNode.className = "empty";
          emptyNode.textContent = "No pipeline events received yet.";
          container.appendChild(emptyNode);
        }
        return;
      }
      if (empty) {
        empty.remove();
      }
      sortedTreeChildKeys(state.executionTree.rootIds).forEach((rootKey) => renderTreeNode(rootKey, container));
      state.executionTree.touchedKeys.clear();
    }

    function renderPipeline() {
      byId("metric-status").textContent = state.status || "idle";
      byId("metric-pipeline-task").textContent = state.taskId || "-";
      byId("metric-active-task").textContent = state.activeTaskId || "-";
      byId("metric-seq").textContent = String(state.lastSequence || 0);
      byId("metric-recovery").textContent = state.recovery || "-";
      byId("pipeline-count").textContent = `${Object.keys(state.executionTree.nodes).length} nodes`;
      const waitingText = state.waitingInput
        ? `Input required:\\n${asPrettyJson(state.waitingInput)}`
        : "No pending input.";
      const permissionText = permissionGuidance(state.latestPermission);
      byId("waiting-input").textContent = permissionText
        ? `${waitingText}\\n\\nPermission:\\n${permissionText}`
        : waitingText;

      renderExecutionTreePatch();
      renderTaskHistory();
    }

    function appendRawJson(container, value) {
      const pre = document.createElement("pre");
      pre.className = "raw-json";
      pre.textContent = asPrettyJson(value) || "No raw data yet.";
      container.appendChild(pre);
    }

    function snapshotEnvelope(value) {
      return value && value.snapshot && typeof value.snapshot === "object" ? value.snapshot : value;
    }

    function appendStateText(parent, className, text, tagName = "span") {
      const element = document.createElement(tagName);
      element.className = className;
      element.textContent = text == null || text === "" ? "-" : String(text);
      parent.appendChild(element);
      return element;
    }

    function snapshotArray(value) {
      return Array.isArray(value) ? value : [];
    }

    function snapshotObject(value) {
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    }

    function stateStatusClass(status) {
      const value = String(status || "").toLowerCase();
      if (["completed", "ready", "approved", "selected"].includes(value)) {
        return "ok";
      }
      if (["failed", "canceled", "denied"].includes(value)) {
        return "danger";
      }
      if (["waiting_input", "input_required", "working", "restarting"].includes(value)) {
        return "warn";
      }
      return "";
    }

    function appendStatePill(parent, text) {
      const pill = appendStateText(parent, ["pill", stateStatusClass(text)].filter(Boolean).join(" "), text);
      return pill;
    }

    function appendStateMetric(parent, label, value) {
      const metric = document.createElement("div");
      metric.className = "state-metric";
      appendStateText(metric, "", label);
      appendStateText(metric, "", value, "strong");
      parent.appendChild(metric);
    }

    function onStateSectionToggle(event) {
      const details = event.currentTarget;
      const key = details && details.getAttribute("data-state-section-key");
      if (!key) {
        return;
      }
      if (details.open) {
        state.expandedStateSectionKeys.add(key);
        state.collapsedStateSectionKeys.delete(key);
      } else {
        state.collapsedStateSectionKeys.add(key);
        state.expandedStateSectionKeys.delete(key);
      }
    }

    function stateSectionOpen(key, defaultOpen) {
      if (state.expandedStateSectionKeys.has(key)) {
        return true;
      }
      if (state.collapsedStateSectionKeys.has(key)) {
        return false;
      }
      return Boolean(defaultOpen);
    }

    function stateSectionKey(title) {
      return String(title || "section").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "section";
    }

    function createStateSection(title, badgeText, options = {}) {
      const key = options.key || `snapshot:${stateSectionKey(title)}`;
      const section = document.createElement("details");
      section.className = "state-section";
      section.setAttribute("data-state-section-key", key);
      section.open = stateSectionOpen(key, options.open !== false);
      section.addEventListener("toggle", onStateSectionToggle);
      const header = document.createElement("summary");
      header.className = "state-section-header";
      appendStateText(header, "", title, "h3");
      if (badgeText !== undefined) {
        appendStatePill(header, badgeText);
      }
      const body = document.createElement("div");
      body.className = "state-section-body";
      section.appendChild(header);
      section.appendChild(body);
      return {section, body};
    }

    function appendStateJson(parent, value) {
      const pre = document.createElement("pre");
      pre.className = "state-json";
      pre.textContent = asPrettyJson(value) || "{}";
      parent.appendChild(pre);
      return pre;
    }

    function appendStateRow(parent, title, meta, value, options = {}) {
      const details = document.createElement("details");
      details.className = "state-row";
      details.open = Boolean(options.open);
      const summary = document.createElement("summary");
      appendStateText(summary, "state-row-title", title);
      appendStateText(summary, "state-row-meta", meta || "");
      if (options.status) {
        appendStatePill(summary, options.status);
      }
      details.appendChild(summary);
      appendStateJson(details, value);
      parent.appendChild(details);
      return details;
    }

    function appendStateEmpty(parent, text) {
      const empty = document.createElement("div");
      empty.className = "state-empty";
      empty.textContent = text;
      parent.appendChild(empty);
    }

    function snapshotCurrentStack(snapshot) {
      const stacks = snapshotObject(snapshot && snapshot.stacks);
      return snapshotObject(stacks.current);
    }

    function pendingInputSummary(snapshot) {
      const input = snapshotObject(snapshot && (snapshot.pendingInput || snapshot.pending_input));
      return input.stepId || input.step_id || input.inputId || input.input_id || "";
    }

    function snapshotNormalHandoff(snapshot) {
      const envelope = snapshotEnvelope(snapshot);
      return snapshotObject(envelope && (envelope.normalHandoff || envelope.normal_handoff));
    }

    function normalHandoffSummary(snapshot) {
      const handoff = snapshotNormalHandoff(snapshot);
      if (Object.keys(handoff).length === 0) {
        return "";
      }
      return [handoff.action, handoff.targetMode || handoff.target_mode, handoff.outcome].filter(Boolean).join(" · ");
    }

    function coordinateLabel(prefix, item) {
      const index = item && item.index ? `${item.index}${item.total ? `/${item.total}` : ""}: ` : "";
      return `${prefix} ${index}${(item && (item.name || item.id || item.runId)) || "unknown"}`;
    }

    function stateNodeMeta(item, extra = "") {
      const parts = [];
      if (item && item.runId) {
        parts.push(`run ${item.runId}`);
      }
      if (item && item.sequence) {
        parts.push(`seq ${item.sequence}`);
      }
      if (extra) {
        parts.push(extra);
      }
      return parts.join(" · ");
    }

    function createSnapshotNode(kind, title, meta, status, value, open = false) {
      const details = document.createElement("details");
      details.className = `state-node state-node-${kind}`;
      details.open = open;
      const summary = document.createElement("summary");
      summary.className = "state-node-summary";
      appendStateText(summary, "state-title", title);
      appendStateText(summary, "state-meta", meta || "");
      appendStatePill(summary, status || "observed");
      details.appendChild(summary);
      const body = document.createElement("div");
      body.className = "state-node-body";
      if (value !== undefined) {
        appendStateJson(body, value);
      }
      details.appendChild(body);
      return {details, body};
    }

    function renderSnapshotCandidateStep(parent, step) {
      const node = createSnapshotNode(
        "candidate-step",
        coordinateLabel("Candidate Step", step),
        stateNodeMeta(step),
        step.status || "pending",
        step,
        false
      );
      parent.appendChild(node.details);
    }

    function renderSnapshotCandidate(parent, candidate) {
      const node = createSnapshotNode(
        "candidate",
        coordinateLabel("Candidate", candidate),
        stateNodeMeta(candidate),
        candidate.status || "observed",
        candidate,
        false
      );
      const steps = snapshotArray(candidate.steps);
      if (steps.length > 0) {
        const children = document.createElement("div");
        children.className = "state-tree";
        steps.forEach((step) => renderSnapshotCandidateStep(children, step));
        node.body.appendChild(children);
      }
      parent.appendChild(node.details);
    }

    function renderSnapshotStep(parent, step) {
      const node = createSnapshotNode(
        "step",
        coordinateLabel("Step", step),
        stateNodeMeta(step),
        step.status || "observed",
        step,
        Boolean(step.status && !["completed", "pending"].includes(step.status))
      );
      const candidates = snapshotArray(step.candidates);
      if (candidates.length > 0) {
        const lanes = document.createElement("div");
        lanes.className = "state-candidate-lanes";
        candidates.forEach((candidate) => renderSnapshotCandidate(lanes, candidate));
        node.body.appendChild(lanes);
      }
      parent.appendChild(node.details);
    }

    function renderSnapshotPipelineTree(container, snapshot) {
      const section = createStateSection("Pipeline State", snapshot.status || "snapshot", {
        key: "snapshot:pipeline-state",
        open: true
      });
      const tree = document.createElement("div");
      tree.className = "state-tree";
      const root = createSnapshotNode(
        "pipeline",
        snapshot.pipelineName || snapshot.pipeline_name || "Pipeline",
        `seq ${snapshot.lastSequence || 0}`,
        snapshot.status || "snapshot",
        {
          taskId: snapshot.taskId,
          contextId: snapshot.contextId,
          lastSequence: snapshot.lastSequence,
          status: snapshot.status
        },
        true
      );
      const steps = snapshotArray(snapshot.steps);
      if (steps.length > 0) {
        steps.forEach((step) => renderSnapshotStep(root.body, step));
      } else {
        appendStateEmpty(root.body, "No steps in snapshot.");
      }
      tree.appendChild(root.details);
      section.body.appendChild(tree);
      container.appendChild(section.section);
    }

    function displayItemTitle(key, item) {
      const detail = snapshotObject(item.detail);
      if (key === "messages") {
        return compactText(item.text || item.message || "message", 90);
      }
      if (key === "candidateDetails") {
        return detail.candidateName || item.candidateName || item.name || item.detailId || "candidate detail";
      }
      if (key === "diagrams") {
        return item.candidateName || item.diagramId || "diagram";
      }
      if (key === "artifacts") {
        return item.filename || item.name || item.artifactId || "artifact";
      }
      if (key === "permissions") {
        return item.toolName || item.permissionId || "permission";
      }
      if (key === "toolResults") {
        return item.toolName || item.toolUseId || "tool result";
      }
      return item.id || item.eventId || key;
    }

    function displayItemMeta(item) {
      return [
        item.status,
        item.decision,
        item.runId,
        item.sequence ? `seq ${item.sequence}` : "",
        item.createdAt
      ].filter(Boolean).join(" · ");
    }

    function renderSnapshotDisplayList(parent, title, key, items) {
      const section = createStateSection(title, `${items.length}`, {
        key: `snapshot:display:${key}`,
        open: !["permissions", "toolResults"].includes(key)
      });
      if (items.length === 0) {
        appendStateEmpty(section.body, `No ${title}.`);
      } else {
        const list = document.createElement("div");
        list.className = "state-row-list";
        items.forEach((item, index) => {
          appendStateRow(
            list,
            displayItemTitle(key, snapshotObject(item)),
            displayItemMeta(snapshotObject(item)) || `#${index + 1}`,
            item
          );
        });
        section.body.appendChild(list);
      }
      parent.appendChild(section.section);
    }

    function renderSnapshotDisplaySections(container, snapshot) {
      const display = snapshotObject(snapshot.display);
      const section = createStateSection("Display Data", "frontend-facing", {
        key: "snapshot:display",
        open: true
      });
      const list = document.createElement("div");
      list.className = "state-row-list";
      [
        ["Messages", "messages"],
        ["Candidate Details", "candidateDetails"],
        ["Diagrams", "diagrams"],
        ["Artifacts", "artifacts"],
        ["Permissions", "permissions"],
        ["Tool Results", "toolResults"]
      ].forEach(([title, key]) => {
        renderSnapshotDisplayList(list, title, key, snapshotArray(display[key]));
      });
      section.body.appendChild(list);
      container.appendChild(section.section);
    }

    function renderSnapshotControlSections(container, snapshot) {
      const section = createStateSection("Stack & Control", "runtime state", {
        key: "snapshot:stack-control",
        open: true
      });
      const infoGrid = document.createElement("div");
      infoGrid.className = "state-info-grid";
      const currentStack = snapshotCurrentStack(snapshot);
      [
        ["Current Stack", currentStack.stackId || currentStack.stack_id || "-"],
        ["Stack Action", currentStack.action || "-"],
        ["Stack Region", currentStack.regionId || currentStack.region_id || "-"],
        ["Pending Input", pendingInputSummary(snapshot) || "-"],
        ["Normal Handoff", normalHandoffSummary(snapshot) || "-"]
      ].forEach(([label, value]) => {
        const info = document.createElement("div");
        info.className = "state-info";
        appendStateText(info, "", label);
        appendStateText(info, "", value, "strong");
        infoGrid.appendChild(info);
      });
      section.body.appendChild(infoGrid);

      const stacks = snapshotObject(snapshot.stacks);
      const control = snapshotObject(snapshot.control);
      const list = document.createElement("div");
      list.className = "state-row-list";
      appendStateRow(list, "current stack", currentStack.stackId || currentStack.stack_id || "none", currentStack);
      appendStateRow(
        list,
        "stack history",
        `${snapshotArray(stacks.history).length} events`,
        snapshotArray(stacks.history)
      );
      appendStateRow(
        list,
        "pending input",
        pendingInputSummary(snapshot) || "none",
        snapshotObject(snapshot.pendingInput || snapshot.pending_input),
        {open: Boolean(pendingInputSummary(snapshot)), status: pendingInputSummary(snapshot) ? "waiting_input" : ""}
      );
      appendStateRow(
        list,
        "normal handoff",
        normalHandoffSummary(snapshot) || "none",
        snapshotNormalHandoff(snapshot),
        {open: Boolean(normalHandoffSummary(snapshot)), status: normalHandoffSummary(snapshot) ? "completed" : ""}
      );
      appendStateRow(
        list,
        "input history",
        `${snapshotArray(control.inputHistory).length} items`,
        snapshotArray(control.inputHistory)
      );
      appendStateRow(
        list,
        "interrupt history",
        `${snapshotArray(control.interruptHistory).length} items`,
        snapshotArray(control.interruptHistory)
      );
      appendStateRow(
        list,
        "rollback history",
        `${snapshotArray(control.rollbackHistory).length} items`,
        snapshotArray(control.rollbackHistory)
      );
      appendStateRow(
        list,
        "candidate restarts",
        `${snapshotArray(control.candidateRestarts).length} items`,
        snapshotArray(control.candidateRestarts)
      );
      appendStateRow(
        list,
        "handoff history",
        `${snapshotArray(control.handoffHistory).length} items`,
        snapshotArray(control.handoffHistory)
      );
      section.body.appendChild(list);
      container.appendChild(section.section);
    }

    function renderSnapshotState(container, value) {
      const snapshot = snapshotEnvelope(value);
      if (!snapshot || typeof snapshot !== "object") {
        appendStateEmpty(container, "No snapshot was fetched yet.");
        return;
      }

      const view = document.createElement("div");
      view.className = "state-view";
      const summary = document.createElement("section");
      summary.className = "state-summary";
      const currentStack = snapshotCurrentStack(snapshot);
      [
        ["Status", snapshot.status || snapshot.state || "snapshot"],
        ["Task", snapshot.taskId || snapshot.task_id || "-"],
        ["Context", snapshot.contextId || snapshot.context_id || "-"],
        ["Last Seq", snapshot.lastSequence || snapshot.sequence || 0],
        ["Pending Input", pendingInputSummary(snapshot) || "-"],
        ["Normal Handoff", normalHandoffSummary(snapshot) || "-"],
        ["Current Stack", currentStack.stackId || currentStack.stack_id || "-"]
      ].forEach(([label, metricValue]) => appendStateMetric(summary, label, metricValue));
      view.appendChild(summary);

      renderSnapshotPipelineTree(view, snapshot);
      renderSnapshotDisplaySections(view, snapshot);
      renderSnapshotControlSections(view, snapshot);
      const full = createStateSection("Full Snapshot JSON", "raw", {
        key: "snapshot:full-json",
        open: false
      });
      appendStateJson(full.body, value);
      view.appendChild(full.section);
      container.appendChild(view);
    }

    function requestPath(value) {
      const path = value && value.path ? String(value.path) : "";
      if (!path) {
        return "/";
      }
      try {
        const parsed = new URL(path, window.location.origin);
        return parsed.pathname;
      } catch {
        return path.split("?")[0] || path;
      }
    }

    function requestEventLabel(row) {
      const value = row.value || {};
      return [value.method || "REQUEST", requestPath(value)].filter(Boolean).join(" ");
    }

    function requestEventSummary(row) {
      const value = row.value || {};
      const payload = value.payload && typeof value.payload === "object" ? value.payload : {};
      if (Object.prototype.hasOwnProperty.call(payload, "prompt")) {
        const prompt = compactText(payload.prompt);
        return prompt ? `prompt: ${prompt}` : "prompt is empty";
      }
      if (payload.taskId) {
        return `taskId: ${payload.taskId}`;
      }
      if (value.path && String(value.path).includes("?")) {
        return String(value.path).split("?")[1];
      }
      return row.error || row.status || "";
    }

    function requestEventMeta(row) {
      const status = row.status === "ok" ? "ok" : row.status === "error" ? "error" : "sent";
      const statusCode = row.statusCode ? String(row.statusCode) : "";
      const error = row.status === "error" ? row.error : "";
      return [status, statusCode, error, row.at].filter(Boolean).join(" · ");
    }

    function appendRequestEventBody(container, row) {
      const body = document.createElement("div");
      body.className = "raw-event-body";
      const pre = document.createElement("pre");
      pre.textContent = asPrettyJson({
        status: row.status,
        statusCode: row.statusCode || undefined,
        error: row.error || undefined,
        request: row.value,
        response: row.response
      });
      body.appendChild(pre);
      container.appendChild(body);
    }

    function appendRawEventBody(container, row) {
      const body = document.createElement("div");
      body.className = "raw-event-body";
      if (row.type === "text_delta_group" || row.type === "a2a_message_group") {
        const textLabel = document.createElement("div");
        textLabel.className = "raw-event-body-label";
        textLabel.textContent = "merged text";
        const text = document.createElement("pre");
        text.className = "raw-event-text-body";
        text.textContent = row.text || "(empty text delta)";
        const jsonLabel = document.createElement("div");
        jsonLabel.className = "raw-event-body-label";
        jsonLabel.textContent = "source events";
        const json = document.createElement("pre");
        json.textContent = asPrettyJson(row.events);
        body.appendChild(textLabel);
        body.appendChild(text);
        body.appendChild(jsonLabel);
        body.appendChild(json);
      } else {
        const pre = document.createElement("pre");
        pre.textContent = asPrettyJson(rawItemValue(row.event || row));
        body.appendChild(pre);
      }
      container.appendChild(body);
    }

    function rawRowKey(row, kind, index) {
      if (kind === "request") {
        const value = row.value || {};
        return ["request", row.at || index, value.method || "", requestPath(value)].join("|");
      }
      if (row.type === "text_delta_group") {
        return ["sse_text", row.key || "", row.firstSequence || row.firstAt || index].join("|");
      }
      if (row.type === "a2a_message_group") {
        return ["sse_message", row.key || "", row.firstAt || index].join("|");
      }
      const item = row.event || row;
      const envelope = pipelineEnvelopeFromRawItem(item);
      if (envelope && (envelope.eventId || envelope.event_id)) {
        return ["sse", envelope.eventId || envelope.event_id].join("|");
      }
      if (envelope && envelope.sequence) {
        return [
          "sse",
          envelope.taskId || envelope.task_id || "",
          envelope.contextId || envelope.context_id || "",
          envelope.sequence
        ].join("|");
      }
      return ["sse", rawItemTimestamp(item) || index].join("|");
    }

    function onRawEventToggle(event) {
      const details = event.currentTarget;
      const key = details && details.getAttribute("data-raw-key");
      if (!key) {
        return;
      }
      if (details.open) {
        state.expandedRawEventKeys.add(key);
      } else {
        state.expandedRawEventKeys.delete(key);
      }
    }

    function renderRequestEvents(container, requests) {
      if (requests.length === 0) {
        const empty = document.createElement("div");
        empty.className = "raw-empty";
        empty.textContent = "No raw data yet.";
        container.appendChild(empty);
        return;
      }

      const list = document.createElement("div");
      list.className = "raw-event-list";
      requests.forEach((row, index) => {
        const key = rawRowKey(row, "request", index);
        const details = document.createElement("details");
        details.className = "raw-event raw-event-request";
        details.setAttribute("data-raw-key", key);
        details.open = state.expandedRawEventKeys.has(key);
        details.addEventListener("toggle", onRawEventToggle);
        if (row.status === "ok") {
          details.className += " raw-event-ok";
        }
        if (row.status === "error") {
          details.className += " raw-event-error";
        }

        const summary = document.createElement("summary");
        const kind = document.createElement("span");
        kind.className = "raw-event-kind";
        kind.textContent = requestEventLabel(row);
        const text = document.createElement("span");
        text.className = "raw-event-text";
        text.title = requestEventSummary(row);
        text.textContent = requestEventSummary(row);
        const meta = document.createElement("span");
        meta.className = "raw-event-meta";
        meta.textContent = requestEventMeta(row);
        summary.appendChild(kind);
        summary.appendChild(text);
        summary.appendChild(meta);
        details.appendChild(summary);
        appendRequestEventBody(details, row);
        list.appendChild(details);
      });
      container.appendChild(list);
    }

    function renderSseEvents(container, events) {
      const rows = groupSseEvents(events);
      if (rows.length === 0) {
        const empty = document.createElement("div");
        empty.className = "raw-empty";
        empty.textContent = "No raw data yet.";
        container.appendChild(empty);
        return;
      }

      const list = document.createElement("div");
      list.className = "raw-event-list";
      rows.forEach((row, index) => {
        const key = rawRowKey(row, "sse", index);
        const details = document.createElement("details");
        details.className = "raw-event";
        details.setAttribute("data-raw-key", key);
        details.open = state.expandedRawEventKeys.has(key);
        details.addEventListener("toggle", onRawEventToggle);
        if (row.type !== "text_delta_group") {
          const envelope = pipelineEnvelopeFromRawItem(row.event);
          const eventType = eventTypeFromRawItem(row.event);
          if (permissionFromEnvelope(envelope)) {
            details.className += " raw-event-permission";
          }
          if (eventType === "error") {
            details.className += " raw-event-error";
          }
        }

        const summary = document.createElement("summary");
        const kind = document.createElement("span");
        kind.className = "raw-event-kind";
        kind.textContent = rawEventLabel(row);
        const text = document.createElement("span");
        text.className = "raw-event-text";
        text.title = rawEventSummary(row);
        text.textContent = rawEventSummary(row);
        const meta = document.createElement("span");
        meta.className = "raw-event-meta";
        meta.textContent = rawEventMeta(row);
        summary.appendChild(kind);
        summary.appendChild(text);
        summary.appendChild(meta);
        details.appendChild(summary);
        appendRawEventBody(details, row);
        list.appendChild(details);
      });
      container.appendChild(list);
    }

    function renderRaw() {
      const rawContainer = byId("raw-output");
      rawContainer.textContent = "";
      rawContainer.className = state.activeRawTab === "snapshot" ? "raw-output state-raw-output" : "raw-output";
      if (state.activeRawTab === "snapshot") {
        renderSnapshotState(rawContainer, state.raw.snapshot);
      } else if (state.activeRawTab === "requests") {
        renderRequestEvents(rawContainer, state.raw.requests);
      } else {
        renderSseEvents(rawContainer, state.raw.sse);
      }

      document.querySelectorAll("[data-raw-tab]").forEach((button) => {
        button.setAttribute("aria-selected", button.dataset.rawTab === state.activeRawTab ? "true" : "false");
      });
    }

    async function readResponseError(response) {
      const text = await response.text();
      const dataLine = text
        .replace(/\\r\\n/g, "\\n")
        .replace(/\\r/g, "\\n")
        .split("\\n")
        .find((line) => line.startsWith("data:"));
      const raw = dataLine ? dataLine.slice(5).trim() : text;
      let body = null;
      try {
        body = raw ? JSON.parse(raw) : null;
      } catch (error) {
        body = {ok: false, error: raw || error.message, body: text};
      }
      if (!body || typeof body !== "object") {
        body = {ok: false, error: String(body || `HTTP ${response.status}`), body: text};
      }
      body.statusCode = response.status;
      return body;
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      if (!response.ok) {
        throw await readResponseError(response);
      }
      const text = await response.text();
      let body = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch (error) {
        body = {ok: false, error: error.message, body: text};
      }
      return body;
    }

    async function healthCheck() {
      const controls = readControls();
      const query = new URLSearchParams({serverUrl: controls.serverUrl});
      const requestRow = appendRawEvent("request", {method: "GET", path: `/api/health?${query}`});
      try {
        const body = await fetchJson(`/api/health?${query}`);
        updateRawRequest(requestRow, {status: "ok", response: body});
        appendRawEvent("sse", {type: "health", body});
        state.status = body.ok ? "healthy" : "unhealthy";
        renderPipeline();
      } catch (error) {
        updateRawRequest(requestRow, {
          status: "error",
          statusCode: error.statusCode,
          error: errorMessage(error),
          response: error
        });
        throw error;
      }
    }

    async function fetchState() {
      const controls = readControls();
      const contextId = controls.contextId || state.contextId;
      const taskId = controls.taskId || state.taskId;
      const params = new URLSearchParams({
        serverUrl: controls.serverUrl,
        contextId,
        taskId,
        afterSequence: String(state.lastSequence || "")
      });
      const requestRow = appendRawEvent("request", {method: "GET", path: `/api/pipeline/state?${params}`});
      try {
        const body = await fetchJson(`/api/pipeline/state?${params}`);
        updateRawRequest(requestRow, {status: "ok", response: body});
        rebuildFromSnapshot(body.snapshot || body);
        if (Array.isArray(body.events)) {
          body.events.forEach((event) => applyPipelineEvent(event));
        }
        appendRawEvent("snapshot", body);
      } catch (error) {
        updateRawRequest(requestRow, {
          status: "error",
          statusCode: error.statusCode,
          error: errorMessage(error),
          response: error
        });
        throw error;
      }
    }

    async function fetchStateIfAvailable() {
      const controls = readControls();
      if (!(controls.contextId || controls.taskId || state.contextId || state.taskId)) {
        return;
      }
      try {
        await fetchState();
      } catch (error) {
        appendRawEvent("sse", {type: "snapshot_error", error: errorMessage(error), body: error});
      }
    }

    async function cancelTask() {
      const controls = readControls();
      const taskId = cancelTaskIdForControls(controls);
      if (!taskId) {
        appendRawEvent("sse", {type: "cancel_error", error: "No active task to cancel."});
        state.status = "error";
        renderPipeline();
        return;
      }
      const payload = {serverUrl: controls.serverUrl, taskId};
      const requestRow = appendRawEvent("request", {method: "POST", path: "/api/task/cancel", payload});
      try {
        const body = await fetchJson("/api/task/cancel", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        updateRawRequest(requestRow, {status: "ok", response: body});
        appendRawEvent("sse", {type: "cancel", body});
        applyPipelineEvent(body);
        await fetchStateIfAvailable();
      } catch (error) {
        updateRawRequest(requestRow, {
          status: "error",
          statusCode: error.statusCode,
          error: errorMessage(error),
          response: error
        });
        throw error;
      }
    }

    async function streamMessage() {
      const controls = readControls();
      const images = await readSelectedImages();
      const payload = {
        serverUrl: controls.serverUrl,
        cwd: controls.cwd,
        contextId: controls.contextId || state.contextId,
        taskId: streamTaskIdForControls(controls),
        prompt: controls.prompt
      };
      if (images.length) {
        payload.images = images;
      }
      const requestRow = appendRawEvent("request", {method: "POST", path: "/api/message/stream", payload});
      state.streamsInFlight += 1;
      state.status = "streaming";
      renderPipeline();

      try {
        const response = await fetch("/api/message/stream", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });

        if (!response.ok) {
          const errorBody = await readResponseError(response);
          const message = errorMessage(errorBody);
          updateRawRequest(requestRow, {
            status: "error",
            statusCode: response.status,
            error: message,
            response: errorBody
          });
          appendRawEvent("sse", {
            type: "error",
            error: errorMessage(errorBody),
            statusCode: response.status,
            body: errorBody
          });
          state.status = "error";
          renderPipeline();
          return;
        }

        updateRawRequest(requestRow, {status: "ok", statusCode: response.status});
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let streamEventCount = 0;
        let shouldStopStream = false;
        while (true) {
          const {done, value} = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, {stream: true});
          buffer = buffer.replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n");
          const chunks = buffer.split("\\n\\n");
          buffer = chunks.pop() || "";
          for (const chunk of chunks) {
            const dataLine = chunk.split("\\n").find((line) => line.startsWith("data:"));
            if (!dataLine) {
              continue;
            }
            const raw = dataLine.slice(5).trim();
            let parsed = raw;
            try {
              parsed = JSON.parse(raw);
            } catch {
              parsed = raw;
            }
            if (parsed && typeof parsed === "object" && (parsed.type === "error" || parsed.error)) {
              state.status = "error";
            }
            const rawRow = appendRawEvent("sse", parsed);
            if (rawRow) {
              applyPipelineEvent(parsed, rawRow, {alreadyRecorded: true});
            }
            await yieldToBrowserAfterStreamEvent(parsed, ++streamEventCount);
            if (shouldStopStreamingAfterPayload(parsed)) {
              shouldStopStream = true;
              await cancelReaderSafely(reader);
              break;
            }
          }
          if (shouldStopStream) {
            break;
          }
        }
        await fetchStateIfAvailable();
        if (state.streamsInFlight === 1 && !state.waitingInput && state.status !== "error") {
          state.status = state.activeTaskId ? "working" : "complete";
        }
      } catch (error) {
        updateRawRequest(requestRow, {status: "error", error: errorMessage(error), response: error});
        throw error;
      } finally {
        state.streamsInFlight = Math.max(0, state.streamsInFlight - 1);
        if (state.streamsInFlight > 0 && state.status !== "error") {
          state.status = "streaming";
        }
        renderPipeline();
      }
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      })[char] || char);
    }

    function serializeExecutionTree() {
      const nodes = {};
      Object.entries(state.executionTree.nodes).forEach(([key, node]) => {
        nodes[key] = {
          key: node.key,
          parentKey: node.parentKey,
          kind: node.kind,
          label: node.label,
          status: node.status,
          meta: node.meta,
          sortOrder: node.sortOrder,
          firstSequence: node.firstSequence,
          children: [...node.children],
          timeline: node.timeline.map((item) => ({...item}))
        };
      });
      return {
        rootIds: [...state.executionTree.rootIds],
        nodes
      };
    }

    function buildExportSnapshot() {
      const controls = readControls();
      return {
        schemaVersion: "iac-code-a2a-debugger-export-v1",
        exportedAt: new Date().toISOString(),
        page: {
          location: window.location.href,
          userAgent: navigator.userAgent
        },
        connection: {
          serverUrl: controls.serverUrl,
          cwd: controls.cwd
        },
        task: {
          taskId: state.taskId || controls.taskId,
          activeTaskId: state.activeTaskId || controls.activeTaskId,
          contextId: state.contextId || controls.contextId,
          status: state.status,
          lastSequence: state.lastSequence,
          recovery: state.recovery
        },
        taskHistory: state.taskHistory.map((item) => ({...item})),
        waitingInput: state.waitingInput,
        latestPermission: state.latestPermission,
        "snapshot": state.raw.snapshot,
        "sseEvents": state.raw.sse,
        "sseEventGroups": groupSseEvents(state.raw.sse),
        "requests": state.raw.requests,
        "executionTree": serializeExecutionTree(),
        "uiState": {
          activeRawTab: state.activeRawTab,
          expandedTreeKeys: [...state.expandedTreeKeys],
          expandedTimelineKeys: [...state.expandedTimelineKeys],
          "expandedRawEventKeys": [...state.expandedRawEventKeys],
          "expandedStateSectionKeys": [...state.expandedStateSectionKeys],
          "collapsedStateSectionKeys": [...state.collapsedStateSectionKeys]
        }
      };
    }

    function restoreSet(values) {
      return new Set(Array.isArray(values) ? values : []);
    }

    function restoreExecutionTree(serializedTree) {
      const tree = createExecutionTree();
      if (!serializedTree || typeof serializedTree !== "object") {
        return tree;
      }
      tree.rootIds = Array.isArray(serializedTree.rootIds) ? [...serializedTree.rootIds] : [];
      const nodes = serializedTree.nodes && typeof serializedTree.nodes === "object" ? serializedTree.nodes : {};
      Object.entries(nodes).forEach(([key, node]) => {
        if (!node || typeof node !== "object") {
          return;
        }
        tree.nodes[key] = {
          key: node.key || key,
          parentKey: node.parentKey || "",
          kind: node.kind || "event",
          label: node.label || node.key || key,
          status: node.status || "observed",
          meta: node.meta || "",
          sortOrder: numericTreeValue(node.sortOrder),
          firstSequence: numericTreeValue(node.firstSequence),
          children: Array.isArray(node.children) ? [...node.children] : [],
          timeline: Array.isArray(node.timeline) ? node.timeline.map((item) => ({...item})) : []
        };
      });
      return tree;
    }

    function rebuildExecutionTreeFromRawEvents(rows) {
      if (!Array.isArray(rows)) {
        return;
      }
      resetPipelineEventDedup();
      rows.forEach((row) => {
        const envelope = pipelineEnvelopeFromRawItem(row);
        if (envelope) {
          appendExecutionTreeEvent(envelope);
        } else {
          appendNormalMessageEvent(row);
        }
      });
    }

    function restoreExportState(data) {
      if (!data || typeof data !== "object") {
        return;
      }
      const task = data.task && typeof data.task === "object" ? data.task : {};
      const uiState = data.uiState && typeof data.uiState === "object" ? data.uiState : {};
      const connection = data.connection && typeof data.connection === "object" ? data.connection : {};

      state.activeRawTab = String(uiState.activeRawTab || state.activeRawTab || "sse");
      state.status = String(task.status || state.status || "snapshot");
      state.taskId = String(task.taskId || "");
      state.activeTaskId = String(task.activeTaskId || "");
      state.taskHistory = Array.isArray(data.taskHistory) ? data.taskHistory.map((item) => ({...item})) : [];
      state.contextId = String(task.contextId || "");
      state.lastSequence = Number(task.lastSequence || 0);
      state.recovery = String(task.recovery || "");
      state.latestPermission = data.latestPermission || null;
      state.waitingInput = data.waitingInput || "";
      const restoredSseEvents = Array.isArray(data.sseEvents) ? data.sseEvents.map((row) => ({...row})) : [];
      state.raw.sse = dedupeRawSseEvents(restoredSseEvents);
      resetPipelineEventDedup();
      state.raw.snapshot = data.snapshot || null;
      state.normalHandoffReady = Boolean(normalHandoffSummary(state.raw.snapshot));
      state.raw.requests = Array.isArray(data.requests) ? data.requests.map((row) => ({...row})) : [];
      state.executionTree = restoreExecutionTree(data.executionTree);
      if (state.executionTree.rootIds.length === 0) {
        rebuildExecutionTreeFromRawEvents(state.raw.sse);
      }
      state.expandedTreeKeys = restoreSet(uiState.expandedTreeKeys);
      state.expandedTimelineKeys = restoreSet(uiState.expandedTimelineKeys);
      state.expandedRawEventKeys = restoreSet(uiState.expandedRawEventKeys);
      state.expandedStateSectionKeys = restoreSet(uiState.expandedStateSectionKeys);
      state.collapsedStateSectionKeys = restoreSet(uiState.collapsedStateSectionKeys);

      if (connection.serverUrl && byId("server-url")) {
        byId("server-url").value = String(connection.serverUrl);
      }
      if (connection.cwd && byId("cwd")) {
        byId("cwd").value = String(connection.cwd);
      }
      syncCapturedIdentityControls();
    }

    function configureExportMode() {
      if (!isExportMode) {
        return;
      }
      document.documentElement.setAttribute("data-export-mode", "true");
      document.body.setAttribute("data-export-mode", "true");
      const subtitle = document.querySelector(".titlebar .subtitle");
      if (subtitle) {
        subtitle.textContent = "Read-only exported debugger snapshot";
      }
      ["server-url", "cwd", "context-id", "task-id", "active-task-id", "prompt"].forEach((id) => {
        const element = byId(id);
        if (element) {
          element.readOnly = true;
        }
      });
      const imageInput = byId("image-input");
      if (imageInput) {
        imageInput.disabled = true;
      }
      ["health-button", "stream-button", "fetch-state-button", "cancel-button"].forEach((id) => {
        const button = byId(id);
        if (button) {
          button.disabled = true;
          button.title = "Disabled in exported read-only snapshots";
        }
      });
    }

    function copyControlValueToClone(clone, id) {
      const source = byId(id);
      const target = clone.querySelector(`#${cssEscape(id)}`);
      if (!source || !target) {
        return;
      }
      if (target.tagName === "TEXTAREA") {
        target.textContent = source.value;
      } else {
        target.setAttribute("value", source.value);
      }
    }

    function cloneDocumentForExport(data, title) {
      const clone = document.documentElement.cloneNode(true);
      clone.setAttribute("data-export-mode", "true");
      const titleElement = clone.querySelector("title");
      if (titleElement) {
        titleElement.textContent = title;
      }
      const body = clone.querySelector("body");
      if (body) {
        body.setAttribute("data-export-mode", "true");
      }
      clone.querySelectorAll("#debug-export-data").forEach((node) => node.remove());
      ["server-url", "cwd", "context-id", "task-id", "active-task-id", "prompt"].forEach((id) => {
        copyControlValueToClone(clone, id);
      });
      ["server-url", "cwd", "context-id", "task-id", "active-task-id", "prompt"].forEach((id) => {
        const element = clone.querySelector(`#${cssEscape(id)}`);
        if (element) {
          element.setAttribute("readonly", "readonly");
        }
      });
      const imageInput = clone.querySelector("#image-input");
      if (imageInput) {
        imageInput.setAttribute("disabled", "disabled");
      }
      ["health-button", "stream-button", "fetch-state-button", "cancel-button"].forEach((id) => {
        const button = clone.querySelector(`#${cssEscape(id)}`);
        if (button) {
          button.setAttribute("disabled", "disabled");
        }
      });

      const embeddedData = JSON.stringify(data).replace(/</g, "\\u003c");
      const dataScript = document.createElement("script");
      dataScript.id = "debug-export-data";
      dataScript.textContent = `window.DEBUGGER_EXPORT_DATA = ${embeddedData};`;
      const firstScript = clone.querySelector("script");
      if (firstScript && firstScript.parentNode) {
        firstScript.parentNode.insertBefore(dataScript, firstScript);
      }
      return clone;
    }

    function buildExportHtml() {
      const data = buildExportSnapshot();
      const title = [
        "iac-code A2A Pipeline Debugger Export",
        data.task.taskId || data.task.contextId || "snapshot"
      ].filter(Boolean).join(" - ");
      const clone = cloneDocumentForExport(data, title);
      return `<!doctype html>
${clone.outerHTML}`;
    }

    function exportCurrentHtmlSnapshot() {
      const html = buildExportHtml();
      const blob = new Blob([html], {type: "text/html;charset=utf-8"});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const id = state.taskId || state.contextId || "snapshot";
      const safeId = String(id).replace(/[^a-zA-Z0-9_.-]+/g, "-").slice(0, 80) || "snapshot";
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      link.href = url;
      link.download = `iac-code-a2a-${safeId}-${stamp}.html`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function withButtonState(button, operation) {
      button.disabled = true;
      Promise.resolve()
        .then(operation)
        .catch((error) => {
          appendRawEvent("sse", {type: "error", error: errorMessage(error), body: error});
          state.status = "error";
          renderPipeline();
        })
        .finally(() => {
          button.disabled = false;
        });
    }

    function withStreamAction(button, operation) {
      button.disabled = false;
      Promise.resolve()
        .then(operation)
        .catch((error) => {
          appendRawEvent("sse", {type: "error", error: errorMessage(error), body: error});
          state.status = "error";
          renderPipeline();
        })
        .finally(() => {
          button.disabled = false;
        });
    }

    document.querySelectorAll("[data-raw-tab]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeRawTab = button.dataset.rawTab;
        renderRaw();
      });
    });
    byId("health-button").addEventListener("click", (event) => withButtonState(event.currentTarget, healthCheck));
    byId("fetch-state-button").addEventListener("click", (event) => withButtonState(event.currentTarget, fetchState));
    byId("cancel-button").addEventListener("click", (event) => withButtonState(event.currentTarget, cancelTask));
    byId("stream-button").addEventListener("click", (event) => withStreamAction(event.currentTarget, streamMessage));
    byId("export-html-button").addEventListener("click", exportCurrentHtmlSnapshot);
    byId("image-input").addEventListener("change", updateImageSummary);

    if (isExportMode) {
      restoreExportState(exportPayload);
      configureExportMode();
    }
    updateImageSummary();
    renderPipeline();
    renderRaw();
  </script>
</body>
</html>"""
    return (
        template.replace("__DEFAULT_SERVER_URL__", default_url)
        .replace("__DEFAULT_CWD__", default_cwd)
        .replace("__DEBUG_LOG_DIR__", debug_log_dir)
        .replace("__DEFAULTS_JSON__", defaults_json)
        .replace("__REPLAY_JSON__", replay_json)
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _query_params(path: str) -> dict[str, str]:
    parsed = urlparse(path)
    values = parse_qs(parsed.query, keep_blank_values=True)
    return {key: items[-1] for key, items in values.items()}


def _send_json(handler: BaseHTTPRequestHandler, status: int, value: Any) -> None:
    body = _json_bytes(value)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _send_html(handler: BaseHTTPRequestHandler, html_body: str) -> None:
    body = html_body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw = handler.rfile.read(int(handler.headers.get("Content-Length", "0") or "0"))
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    return data


def _proxy_error(result: ProxyResult) -> dict[str, Any]:
    return {
        "ok": False,
        "statusCode": result.status_code,
        "error": result.error or "Target server returned a non-JSON response",
        "body": result.data if result.data is not None else result.text[:1000],
    }


def _health_response(server_url: str) -> tuple[int, dict[str, Any]]:
    base_url = normalize_server_url(server_url)
    health = fetch_json(f"{base_url}/health")
    if health.status_code != 200 or health.error is not None or health.data is None:
        return 502, _proxy_error(health)
    card = fetch_json(f"{base_url}/.well-known/agent-card.json")
    if card.status_code != 200 or card.error is not None or card.data is None:
        return 502, _proxy_error(card)
    return 200, {"ok": True, "health": health.data, "agentCard": card.data}


def _pipeline_state_response(params: dict[str, str]) -> tuple[int, Any]:
    server_url = params.get("serverUrl", "")
    context_id = params.get("contextId", "")
    task_id = params.get("taskId", "")
    after_sequence = params.get("afterSequence", "")
    if not context_id and not task_id:
        return 400, {"ok": False, "error": "contextId or taskId is required"}
    base_url = normalize_server_url(server_url)
    query_items: dict[str, str] = {}
    if context_id:
        query_items["contextId"] = context_id
    if task_id:
        query_items["taskId"] = task_id
    if after_sequence:
        query_items["afterSequence"] = after_sequence
    result = fetch_json(f"{base_url}/iac-code/pipeline/state?{urlencode(query_items)}")
    if result.status_code != 200 or result.error is not None or result.data is None:
        return 502, _proxy_error(result)
    return 200, result.data


def _task_get_response(params: dict[str, str]) -> tuple[int, Any]:
    task_id = params.get("taskId", "").strip()
    if not task_id:
        return 400, {"ok": False, "error": "taskId is required"}
    server_url = normalize_server_url(params.get("serverUrl", ""))
    raw_history_length = params.get("historyLength", "").strip()
    history_length = None
    if raw_history_length:
        try:
            history_length = int(raw_history_length)
        except ValueError:
            return 400, {"ok": False, "error": "historyLength must be an integer"}
        if history_length < 0:
            return 400, {"ok": False, "error": "historyLength must be greater than or equal to 0"}
    result = fetch_json(
        f"{server_url}/",
        method="POST",
        payload=build_task_get_payload(task_id=task_id, history_length=history_length),
    )
    if result.status_code != 200 or result.error is not None or result.data is None:
        return 502, _proxy_error(result)
    return 200, result.data


def _task_cancel_response(body: dict[str, Any]) -> tuple[int, Any]:
    task_id_value = body.get("taskId", "")
    task_id = "" if task_id_value is None else str(task_id_value).strip()
    if not task_id:
        return 400, {"ok": False, "error": "taskId is required"}
    server_url = normalize_server_url(str(body.get("serverUrl", "")))
    result = fetch_json(
        f"{server_url}/",
        method="POST",
        payload=build_task_cancel_payload(task_id=task_id),
    )
    if result.status_code != 200 or result.error is not None or result.data is None:
        return 502, _proxy_error(result)
    return 200, result.data


def _open_sse_stream(server_url: str, payload: dict[str, Any], timeout: float = 300.0):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{server_url}/",
        data=body,
        headers={"Content-Type": "application/json", **A2A_VERSION_HEADERS},
        method="POST",
    )
    return urlopen(request, timeout=timeout)


def _message_stream_body(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    server_url = normalize_server_url(str(body.get("serverUrl", "")))
    cwd = str(body.get("cwd", ""))
    prompt = str(body.get("prompt", ""))
    context_id = str(body.get("contextId", ""))
    task_id = str(body.get("taskId", ""))
    if not cwd:
        raise ValueError("cwd is required")
    if not prompt and not body.get("images"):
        raise ValueError("prompt or image is required")
    payload = build_message_stream_payload(
        cwd=cwd,
        prompt=prompt,
        context_id=context_id,
        task_id=task_id,
        request_id=str(uuid.uuid4()),
        message_id=str(uuid.uuid4()),
        images=body.get("images"),
    )
    return server_url, payload


def _is_client_disconnect_error(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if not isinstance(exc, OSError):
        return False
    return getattr(exc, "errno", None) in {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED} or getattr(
        exc,
        "winerror",
        None,
    ) in {10053, 10054}


def _send_sse_error(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    body = f"data: {json.dumps({'ok': False, 'error': message}, ensure_ascii=False)}\n\n".encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError as exc:
        if _is_client_disconnect_error(exc):
            return
        raise


def _send_sse_event(handler: BaseHTTPRequestHandler, status: int, event: dict[str, Any]) -> None:
    body = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError as exc:
        if _is_client_disconnect_error(exc):
            return
        raise


def _jsonrpc_error_message(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        recoverable_task_id = _recoverable_task_id_from_jsonrpc_error(error)
        if isinstance(message, str) and message:
            if recoverable_task_id:
                return f"{message} Resume task {recoverable_task_id}."
            return message
        return json.dumps(error, ensure_ascii=False)
    if isinstance(error, str) and error:
        return error
    return None


def _recoverable_task_id_from_jsonrpc_error(error: dict[str, Any]) -> str | None:
    data = error.get("data")
    if isinstance(data, dict):
        task_id = data.get("recoverableTaskId")
        return task_id if isinstance(task_id, str) and task_id else None
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if isinstance(metadata, dict):
                task_id = metadata.get("recoverableTaskId")
                if isinstance(task_id, str) and task_id:
                    return task_id
    return None


def create_server(config: DebuggerConfig) -> ThreadingHTTPServer:
    class A2APipelineDebuggerHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    _send_html(self, render_index_html(config))
                    return
                if parsed.path == "/api/health":
                    append_debug_log(config, "request", {"method": "GET", "path": self.path})
                    status, body = _health_response(_query_params(self.path).get("serverUrl", ""))
                    append_debug_log(config, "sse" if status == 200 else "error", body)
                    _send_json(self, status, body)
                    return
                if parsed.path == "/api/pipeline/state":
                    append_debug_log(config, "request", {"method": "GET", "path": self.path})
                    status, body = _pipeline_state_response(_query_params(self.path))
                    append_debug_log(config, "snapshot" if status == 200 else "error", body)
                    _send_json(self, status, body)
                    return
                if parsed.path == "/api/task/get":
                    append_debug_log(config, "request", {"method": "GET", "path": self.path})
                    status, body = _task_get_response(_query_params(self.path))
                    append_debug_log(config, "sse" if status == 200 else "error", body)
                    _send_json(self, status, body)
                    return
            except ValueError as exc:
                body = {"ok": False, "error": str(exc)}
                append_debug_log(config, "error", body)
                _send_json(self, 400, body)
                return
            _send_json(self, 404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = _read_json_body(self)
                if parsed.path == "/api/message/stream":
                    append_debug_log(config, "request", {"method": "POST", "path": self.path, "payload": body})
                    server_url, payload = _message_stream_body(body)
                    try:
                        with _open_sse_stream(server_url, payload) as response:
                            content_type = str(response.headers.get("Content-Type", "")).lower()
                            if "text/event-stream" not in content_type:
                                raw = response.read()
                                data, _text = _decode_json_text(raw)
                                message = _jsonrpc_error_message(data)
                                if message:
                                    event = {
                                        "type": "error",
                                        "error": message,
                                        "statusCode": response.status,
                                        "body": data,
                                    }
                                    append_debug_log(config, "sse", event)
                                    _send_sse_event(self, 200, event)
                                    return
                                append_debug_log(
                                    config,
                                    "error",
                                    {
                                        "ok": False,
                                        "error": "Target server returned a non-SSE response",
                                        "statusCode": response.status,
                                    },
                                )
                                _send_sse_error(self, 502, "Target server returned a non-SSE response")
                                return
                            self.send_response(response.status)
                            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                            self.end_headers()
                            event_count = 0
                            for line in response:
                                parsed_event = _parse_sse_data_line(line)
                                if parsed_event is not None:
                                    event_count += 1
                                    append_debug_log(config, "sse", parsed_event)
                                try:
                                    self.wfile.write(line)
                                    self.wfile.flush()
                                except OSError as exc:
                                    if _is_client_disconnect_error(exc):
                                        return
                                    raise
                            if event_count == 0:
                                append_debug_log(config, "sse", {"type": "stream_empty", "statusCode": response.status})
                    except HTTPError as exc:
                        append_debug_log(config, "error", {"ok": False, "error": f"HTTP {exc.code}"})
                        _send_sse_error(self, 502, f"HTTP {exc.code}")
                    except (TimeoutError, URLError, OSError) as exc:
                        if _is_client_disconnect_error(exc):
                            return
                        error = str(exc)
                        append_debug_log(config, "error", {"ok": False, "error": error})
                        _send_sse_error(self, 502, error)
                    return
                if parsed.path == "/api/task/cancel":
                    append_debug_log(config, "request", {"method": "POST", "path": self.path, "payload": body})
                    status, response_body = _task_cancel_response(body)
                    append_debug_log(
                        config,
                        "sse" if status == 200 else "error",
                        {"type": "cancel", "body": response_body},
                    )
                    _send_json(self, status, response_body)
                    return
            except ValueError as exc:
                body = {"ok": False, "error": str(exc)}
                append_debug_log(config, "error", body)
                _send_json(self, 400, body)
                return
            _send_json(self, 404, {"ok": False, "error": "Not found"})

    return ThreadingHTTPServer((config.host, config.port), A2APipelineDebuggerHandler)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = DebuggerConfig(
        host=args.host,
        port=args.port,
        default_server_url=normalize_server_url(args.default_server_url),
        default_cwd=args.default_cwd,
        log_dir=str(create_debug_log_dir(args.log_dir or None)),
        replay_export=load_debug_log_export(args.load_log_dir) if args.load_log_dir else None,
    )
    server = create_server(config)
    host, port = server.server_address[:2]
    print(f"A2A pipeline debugger listening on http://{host}:{port}", flush=True)
    print(f"A2A pipeline debugger logs: {config.log_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
