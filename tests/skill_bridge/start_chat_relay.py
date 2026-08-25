"""Offline ROS chat relay used by the external Skill integration tests.

The HTTP surface deliberately mirrors only the published ROS StartChat and
StopChat OpenAPIs. Test configuration such as the A2A URL and workspace is
injected into the server constructor and is never accepted from an HTTP request.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import queue
import re
import signal
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

START_CHAT_PARAMETERS = frozenset(
    {
        "Query",
        "SessionId",
        "EnablePartialMessage",
        "Mode",
        "Attachments",
        "AgentVersion",
        "EnableThinking",
        "RegionId",
        "ClientContext",
    }
)
STOP_CHAT_PARAMETERS = frozenset({"SessionId", "AgentVersion"})
RPC_SYSTEM_PARAMETERS = frozenset(
    {
        "AccessKeyId",
        "Action",
        "Format",
        "RegionId",
        "SecurityToken",
        "Signature",
        "SignatureMethod",
        "SignatureNonce",
        "SignatureType",
        "SignatureVersion",
        "Timestamp",
        "Version",
    }
)
_ATTACHMENT_PARAMETER = re.compile(r"Attachments\.[1-9][0-9]*\.(?:Type|MimeType|Name|OssObjectKey)\Z")
_BOOLEAN_VALUES = frozenset({"true", "false"})
_MODES = frozenset({"IaCCodeNormal", "IaCCodePipeline"})
_TERMINAL_TASK_STATES = frozenset(
    {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
)
PERMISSION_QUERY_PREFIX = "IAC_CODE_PERMISSION:"
_MAX_UPSTREAM_NON_SSE_BYTES = 1024 * 1024
_END = object()


class StartChatRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _single_value_parameters(path: str, body: bytes) -> dict[str, str]:
    split = urlsplit(path)
    if split.path != "/":
        raise StartChatRequestError(
            "InvalidAction.NotFound",
            "ROS chat actions are only available at the RPC root path.",
        )
    combined: dict[str, list[str]] = {}
    for encoded in (split.query, body.decode("utf-8") if body else ""):
        for key, values in parse_qs(encoded, keep_blank_values=True).items():
            combined.setdefault(key, []).extend(values)
    repeated = sorted(key for key, values in combined.items() if len(values) != 1)
    if repeated:
        raise StartChatRequestError("InvalidParameter", "Repeated parameter: {}".format(repeated[0]))
    return {key: values[0] for key, values in combined.items()}


def parse_start_chat_request(path: str, body: bytes, headers: Any) -> dict[str, str]:
    """Validate the exact OpenAPI request surface and return business parameters."""

    parameters = _single_value_parameters(path, body)
    action = parameters.get("Action") or headers.get("x-acs-action")
    if action != "StartChat":
        raise StartChatRequestError("InvalidAction", "Action must be StartChat.")
    unknown = sorted(
        key
        for key in parameters
        if key not in RPC_SYSTEM_PARAMETERS
        and key not in START_CHAT_PARAMETERS
        and _ATTACHMENT_PARAMETER.fullmatch(key) is None
    )
    if unknown:
        raise StartChatRequestError("InvalidParameter", "Unknown StartChat parameter: {}".format(unknown[0]))
    query = parameters.get("Query")
    if query is None or not query.strip():
        raise StartChatRequestError("InvalidParameter.Query", "Query is required.")
    mode = parameters.get("Mode", "IaCCodeNormal")
    if mode not in _MODES:
        raise StartChatRequestError("InvalidParameter.Mode", "Mode is not supported.")
    for name in ("EnablePartialMessage", "EnableThinking"):
        value = parameters.get(name)
        if value is not None and value.lower() not in _BOOLEAN_VALUES:
            raise StartChatRequestError("InvalidParameter.{}".format(name), "{} must be true or false.".format(name))
    agent_version = parameters.get("AgentVersion")
    if agent_version not in (None, "V2"):
        raise StartChatRequestError("InvalidParameter.AgentVersion", "Only AgentVersion V2 is supported.")
    if parameters.get("ClientContext") and mode != "IaCCodeNormal":
        raise StartChatRequestError(
            "InvalidParameter.ClientContextMode",
            "ClientContext is only supported in IaCCodeNormal mode.",
        )
    return {
        key: value
        for key, value in parameters.items()
        if key in START_CHAT_PARAMETERS or _ATTACHMENT_PARAMETER.fullmatch(key) is not None
    }


def parse_stop_chat_request(path: str, body: bytes, headers: Any) -> dict[str, str]:
    """Validate the exact StopChat OpenAPI request surface."""

    parameters = _single_value_parameters(path, body)
    action = parameters.get("Action") or headers.get("x-acs-action")
    if action != "StopChat":
        raise StartChatRequestError("InvalidAction", "Action must be StopChat.")
    unknown = sorted(key for key in parameters if key not in RPC_SYSTEM_PARAMETERS and key not in STOP_CHAT_PARAMETERS)
    if unknown:
        raise StartChatRequestError("InvalidParameter", "Unknown StopChat parameter: {}".format(unknown[0]))
    session_id = parameters.get("SessionId")
    if session_id is None or not session_id.strip():
        raise StartChatRequestError("InvalidParameter.SessionId", "SessionId is required.")
    agent_version = parameters.get("AgentVersion")
    if agent_version not in (None, "V2"):
        raise StartChatRequestError("InvalidParameter.AgentVersion", "Only AgentVersion V2 is supported.")
    return {key: value for key, value in parameters.items() if key in STOP_CHAT_PARAMETERS}


def _permission_query(value: str) -> dict[str, Any] | None:
    if not value.startswith(PERMISSION_QUERY_PREFIX):
        return None
    try:
        payload = json.loads(value[len(PERMISSION_QUERY_PREFIX) :].lstrip())
    except ValueError:
        return None
    if isinstance(payload, dict) and payload.get("schemaVersion") == 1 and payload.get("kind") == "permission":
        return payload
    return None


def _event_payload(value: dict[str, Any]) -> dict[str, Any]:
    result = value.get("result")
    if not isinstance(result, dict):
        return value
    for key in ("statusUpdate", "artifactUpdate", "task"):
        nested = result.get(key)
        if isinstance(nested, dict):
            return nested
    return result


def _iac_code_metadata(value: dict[str, Any]) -> dict[str, Any] | None:
    payload = _event_payload(value)
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    iac_code = metadata.get("iac_code") if isinstance(metadata, dict) else None
    return iac_code if isinstance(iac_code, dict) else None


def _pipeline_envelopes(iac_code: dict[str, Any]) -> list[dict[str, Any]]:
    pipeline = iac_code.get("pipeline")
    if isinstance(pipeline, dict):
        return [pipeline]
    batch = iac_code.get("pipelineBatch")
    events = batch.get("events") if isinstance(batch, dict) else None
    return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []


def _sideband_input(value: dict[str, Any]) -> dict[str, Any] | None:
    iac_code = _iac_code_metadata(value)
    if iac_code is None:
        return None
    input_value = iac_code.get("input")
    if not isinstance(input_value, dict) or input_value.get("kind") != "permission":
        return None
    if any(
        envelope.get("eventType") == "permission_requested" and envelope.get("status") == "working"
        for envelope in _pipeline_envelopes(iac_code)
    ):
        return input_value
    return None


def _normal_handoff_ready(value: dict[str, Any]) -> bool:
    iac_code = _iac_code_metadata(value)
    if iac_code is None:
        return False
    return any(
        envelope.get("eventType") == "pipeline_handoff_ready"
        and envelope.get("visibility") in {None, "committed"}
        and isinstance(envelope.get("data"), dict)
        and envelope["data"].get("action") == "switch_to_normal"
        and envelope["data"].get("targetMode") == "normal"
        for envelope in _pipeline_envelopes(iac_code)
    )


def _permission_ack_input_id(value: dict[str, Any]) -> str | None:
    def find(item: Any) -> str | None:
        if isinstance(item, dict):
            if item.get("kind") == "permission_ack" and item.get("accepted") is True:
                input_id = item.get("inputId")
                return input_id if isinstance(input_id, str) and input_id else None
            for candidate in item.values():
                found = find(candidate)
                if found:
                    return found
        elif isinstance(item, list):
            for candidate in item:
                found = find(candidate)
                if found:
                    return found
        return None

    return find(value)


def _event_task_id(value: dict[str, Any]) -> str | None:
    def find(item: Any) -> str | None:
        if isinstance(item, dict):
            candidate = item.get("taskId")
            if isinstance(candidate, str) and candidate:
                return candidate
            for candidate in item.values():
                found = find(candidate)
                if found:
                    return found
        elif isinstance(item, list):
            for candidate in item:
                found = find(candidate)
                if found:
                    return found
        return None

    return find(value)


def _event_task_state(value: dict[str, Any]) -> str | None:
    payload = _event_payload(value)
    status = payload.get("status") if isinstance(payload, dict) else None
    state = status.get("state") if isinstance(status, dict) else None
    if not isinstance(state, str) and isinstance(payload, dict):
        state = payload.get("state")
    return state if isinstance(state, str) and state else None


def _is_serial_input_boundary(value: dict[str, Any]) -> bool:
    iac_code = _iac_code_metadata(value)
    if iac_code is None or not isinstance(iac_code.get("input"), dict):
        return False
    return _sideband_input(value) is None and not iac_code.get("pendingPermissions")


def _upstream_failure(session_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "id": session_id,
        "object": "response",
        "status": "failed",
        "error": {"code": code, "message": message},
    }


@dataclass
class _UpstreamCall:
    events: queue.Queue[object] = field(default_factory=queue.Queue)
    acknowledged_input_ids: set[str] = field(default_factory=set)
    thread: threading.Thread | None = None
    last_task_state: str | None = None


@dataclass
class _Session:
    session_id: str
    mode: str
    task_id: str | None = None
    active_call: _UpstreamCall | None = None
    pending_sideband: dict[str, dict[str, Any]] = field(default_factory=dict)
    normal_handoff_ready: bool = False
    state_lock: threading.Lock = field(default_factory=threading.Lock)


class StartChatRelay(ThreadingHTTPServer):
    """HTTPS RPC relay limited to the StartChat and StopChat OpenAPIs."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        a2a_url: str,
        pipeline_a2a_url: str | None = None,
        workspace: str,
        ssl_context: ssl.SSLContext,
        upstream_timeout: float = 15.0,
        heartbeat_interval: float = 15.0,
        metrics_path: str | None = None,
    ) -> None:
        super().__init__(server_address, _StartChatHandler)
        self.a2a_url = a2a_url
        self.pipeline_a2a_url = pipeline_a2a_url or a2a_url
        self.workspace = workspace
        self.upstream_timeout = upstream_timeout
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self.heartbeat_interval = heartbeat_interval
        self.sessions: dict[str, _Session] = {}
        self.sessions_lock = threading.Lock()
        self.metrics_path = pathlib.Path(metrics_path) if metrics_path else None
        self.metrics_lock = threading.Lock()
        self.request_metrics: list[dict[str, Any]] = []
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.socket = ssl_context.wrap_socket(self.socket, server_side=True)

    def begin_request_metric(self, session: _Session, parameters: dict[str, str]) -> dict[str, Any]:
        metric: dict[str, Any] = {
            "action": "StartChat",
            "startedAtUnixMs": int(time.time() * 1000),
            "sessionId": session.session_id,
            "mode": parameters.get("Mode", "IaCCodeNormal"),
            "queryBytes": len(parameters["Query"].encode("utf-8")),
            "queryKind": "permission" if _permission_query(parameters["Query"]) is not None else "conversation",
            "returnedEventCount": 0,
            "returnedSseBytes": 0,
            "eventKinds": {},
        }
        with self.metrics_lock:
            self.request_metrics.append(metric)
        return metric

    def begin_stop_metric(self, parameters: dict[str, str]) -> dict[str, Any]:
        metric: dict[str, Any] = {
            "action": "StopChat",
            "startedAtUnixMs": int(time.time() * 1000),
            "sessionId": parameters["SessionId"],
        }
        with self.metrics_lock:
            self.request_metrics.append(metric)
        return metric

    def finish_request_metric(self, metric: dict[str, Any]) -> None:
        metric["finishedAtUnixMs"] = int(time.time() * 1000)
        metric["durationMs"] = metric["finishedAtUnixMs"] - metric["startedAtUnixMs"]
        self._write_metrics()

    def _write_metrics(self) -> None:
        if self.metrics_path is None:
            return
        with self.metrics_lock:
            payload = {"schemaVersion": 1, "requests": self.request_metrics}
            self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.metrics_path.with_suffix(self.metrics_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.metrics_path)

    def resolve_session(self, parameters: dict[str, str]) -> tuple[_Session, bool]:
        requested = parameters.get("SessionId")
        mode = parameters.get("Mode", "IaCCodeNormal")
        with self.sessions_lock:
            if requested:
                session = self.sessions.get(requested)
                if session is None:
                    raise StartChatRequestError("SessionNotFound", "The requested SessionId does not exist.")
                if session.mode != mode:
                    raise StartChatRequestError("InvalidParameter.Mode", "A session cannot change mode.")
                return session, False
            session_id = str(uuid.uuid4())
            session = _Session(session_id=session_id, mode=mode)
            self.sessions[session_id] = session
            return session, True

    def start_a2a_call(self, session: _Session, parameters: dict[str, str]) -> _UpstreamCall:
        query = parameters["Query"]
        message: dict[str, Any] = {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "contextId": session.session_id,
            "parts": [{"text": query}],
            "metadata": {
                "iac_code": {
                    "cwd": self.workspace,
                    "thinking": {"enabled": parameters.get("EnableThinking", "true").lower() == "true"},
                }
            },
        }
        region_id = parameters.get("RegionId")
        if region_id:
            message["metadata"]["iac_code"]["alibaba_cloud_region_id"] = region_id
        if session.mode == "IaCCodePipeline":
            # The ROS Agent gateway requests iac-code's rich A2A candidate
            # projection for Pipeline turns. This is internal gateway metadata,
            # not an additional StartChat parameter or HTTP capability.
            message["metadata"]["iac_code"]["candidatePresentation"] = "rich-v1"
        # A text-only gateway can omit the outer taskId for the JSON permission
        # response. iac-code must recover it from requestTaskId after parsing.
        if session.task_id and _permission_query(query) is None and not session.normal_handoff_ready:
            message["taskId"] = session.task_id
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "SendStreamingMessage",
            "params": {
                "message": message,
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        }
        call = _UpstreamCall()
        upstream_url = self.pipeline_a2a_url if session.mode == "IaCCodePipeline" else self.a2a_url
        call.thread = threading.Thread(
            target=self._consume_a2a,
            args=(session, call, payload, upstream_url),
            name="start-chat-a2a-stream",
            daemon=True,
        )
        call.thread.start()
        return call

    def stop_session(self, session_id: str) -> str:
        with self.sessions_lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise StartChatRequestError("SessionNotFound", "The requested SessionId does not exist.")
        with session.state_lock:
            task_id = session.task_id
            active_call = session.active_call
            last_state = active_call.last_task_state if active_call is not None else None
        if not task_id or last_state in _TERMINAL_TASK_STATES:
            return "NoActiveStream"
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "CancelTask",
            "params": {"id": task_id},
        }
        upstream_url = self.pipeline_a2a_url if session.mode == "IaCCodePipeline" else self.a2a_url
        request = Request(
            upstream_url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"A2A-Version": "1.0", "Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=self.upstream_timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        except (HTTPError, OSError, URLError):
            return "Failed"
        if len(raw) > 1024 * 1024:
            return "Failed"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            return "Failed"
        if not isinstance(value, dict):
            return "Failed"
        error = value.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").lower()
            return "NoActiveStream" if "cannot be canceled" in message or "not found" in message else "Failed"
        state = _event_task_state(value)
        if state == "TASK_STATE_CANCELED":
            return "Stopped"
        return "Stopping" if isinstance(value.get("result"), dict) else "Failed"

    def _consume_a2a(
        self,
        session: _Session,
        call: _UpstreamCall,
        payload: dict[str, Any],
        upstream_url: str,
    ) -> None:
        request = Request(
            upstream_url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"A2A-Version": "1.0", "Accept": "text/event-stream", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            non_sse_body = bytearray()
            saw_sse_event = False
            with build_opener(ProxyHandler({})).open(request, timeout=self.upstream_timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        remaining = _MAX_UPSTREAM_NON_SSE_BYTES - len(non_sse_body)
                        if not saw_sse_event and remaining > 0:
                            non_sse_body.extend(raw_line[:remaining])
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    saw_sse_event = True
                    task_id = _event_task_id(event)
                    if task_id:
                        with session.state_lock:
                            session.task_id = task_id
                    task_state = _event_task_state(event)
                    if task_state:
                        call.last_task_state = task_state
                    self._observe_sideband_state(session, event)
                    ack_input_id = _permission_ack_input_id(event)
                    if ack_input_id:
                        call.acknowledged_input_ids.add(ack_input_id)
                    call.events.put(event)
            if not saw_sse_event:
                code = "A2AEmptyStream"
                message = "A2A ended without an SSE event."
                try:
                    value = json.loads(non_sse_body.decode("utf-8"))
                except (UnicodeError, ValueError):
                    value = None
                error = value.get("error") if isinstance(value, dict) else None
                if isinstance(error, dict):
                    code = " ".join(str(error.get("code") or "A2AJsonRpcError").split())[:160]
                    message = " ".join(str(error.get("message") or "A2A returned a JSON-RPC error.").split())[:2000]
                call.events.put(_upstream_failure(session.session_id, code, message))
        except HTTPError as exc:
            call.events.put(
                _upstream_failure(
                    session.session_id,
                    "A2AHttpError",
                    "A2A returned HTTP {}.".format(exc.code),
                )
            )
        except (OSError, URLError) as exc:
            call.events.put(_upstream_failure(session.session_id, "A2AConnectionError", str(exc)))
        finally:
            call.events.put(_END)

    @staticmethod
    def _observe_sideband_state(session: _Session, event: dict[str, Any]) -> None:
        iac_code = _iac_code_metadata(event)
        if iac_code is None:
            return
        direct = _sideband_input(event)
        pending = iac_code.get("pendingPermissions")
        with session.state_lock:
            if _normal_handoff_ready(event):
                session.normal_handoff_ready = True
            if isinstance(pending, list):
                session.pending_sideband = {
                    input_id: dict(item)
                    for item in pending
                    if isinstance(item, dict) and isinstance((input_id := item.get("inputId")), str) and input_id
                }
            if direct is not None:
                input_id = direct.get("inputId")
                if isinstance(input_id, str) and input_id:
                    session.pending_sideband[input_id] = dict(direct)


class _StartChatHandler(BaseHTTPRequestHandler):
    server: StartChatRelay
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self._request_metric: dict[str, Any] | None = None
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                parameters = _single_value_parameters(self.path, body)
                action = parameters.get("Action") or self.headers.get("x-acs-action")
            except (StartChatRequestError, UnicodeDecodeError, ValueError) as exc:
                error = (
                    exc
                    if isinstance(exc, StartChatRequestError)
                    else StartChatRequestError("InvalidParameter", str(exc))
                )
                self._json_error(error)
                return
            if action == "StopChat":
                self._do_stop_chat(body)
            else:
                self._do_start_chat(body)
        finally:
            if self._request_metric is not None:
                self.server.finish_request_metric(self._request_metric)

    def _do_start_chat(self, body: bytes) -> None:
        try:
            parameters = parse_start_chat_request(self.path, body, self.headers)
            session, _created = self.server.resolve_session(parameters)
            self._request_metric = self.server.begin_request_metric(session, parameters)
        except StartChatRequestError as error:
            self._json_error(error)
            return
        except (UnicodeDecodeError, ValueError) as exc:
            error = StartChatRequestError("InvalidParameter", str(exc))
            self._json_error(error)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        self._active_session = session
        permission_payload = _permission_query(parameters["Query"])
        if permission_payload is not None:
            input_id = permission_payload.get("inputId")
            with session.state_lock:
                is_sideband = isinstance(input_id, str) and input_id in session.pending_sideband
            response_call = self.server.start_a2a_call(session, parameters)
            self._relay_until_end(response_call)
            if is_sideband and isinstance(input_id, str) and input_id in response_call.acknowledged_input_ids:
                with session.state_lock:
                    session.pending_sideband.pop(input_id, None)
            if not is_sideband and session.mode != "IaCCodePipeline":
                with session.state_lock:
                    parent_call = session.active_call
                if parent_call is not None:
                    parent_ended = self._relay_until_serial_boundary(parent_call)
                    if parent_ended:
                        with session.state_lock:
                            if session.active_call is parent_call:
                                session.active_call = None
            return

        call = self.server.start_a2a_call(session, parameters)
        with session.state_lock:
            owns_parent_stream = session.active_call is None
            if owns_parent_stream:
                session.active_call = call
        if not owns_parent_stream:
            self._relay_until_end(call)
            return
        parent_ended = (
            self._relay_until_end(call)
            if session.mode == "IaCCodePipeline"
            else self._relay_until_serial_boundary(call)
        )
        if parent_ended:
            with session.state_lock:
                if session.active_call is call:
                    session.active_call = None

    def _do_stop_chat(self, body: bytes) -> None:
        try:
            parameters = parse_stop_chat_request(self.path, body, self.headers)
            self._request_metric = self.server.begin_stop_metric(parameters)
            status = self.server.stop_session(parameters["SessionId"])
        except StartChatRequestError as error:
            if self._request_metric is not None:
                self._request_metric["errorCode"] = error.code
            self._json_error(error)
            return
        self._request_metric["stopStatus"] = status
        response = json.dumps(
            {"Status": status, "SessionId": parameters["SessionId"], "RequestId": str(uuid.uuid4())},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)
        self.close_connection = True

    def _write_event(self, event: dict[str, Any]) -> bool:
        try:
            data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.wfile.write(b"data: " + data + b"\n\n")
            self.wfile.flush()
            self._observe_returned_event(event, len(data) + 8)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _relay_until_end(self, call: _UpstreamCall) -> bool:
        while True:
            try:
                item = call.events.get(timeout=self.server.heartbeat_interval)
            except queue.Empty:
                if not self._write_event({"object": "heartbeat"}):
                    return False
                continue
            if item is _END:
                return True
            assert isinstance(item, dict)
            if not self._write_event(item):
                return False

    def _relay_until_serial_boundary(self, call: _UpstreamCall) -> bool:
        while True:
            try:
                item = call.events.get(timeout=self.server.heartbeat_interval)
            except queue.Empty:
                if not self._write_event({"object": "heartbeat"}):
                    return False
                continue
            if item is _END:
                return True
            assert isinstance(item, dict)
            if not self._write_event(item):
                return False
            if _is_serial_input_boundary(item):
                return False

    def _observe_returned_event(self, event: dict[str, Any], wire_bytes: int) -> None:
        if self._request_metric is None:
            return
        self._request_metric["returnedEventCount"] += 1
        self._request_metric["returnedSseBytes"] += wire_bytes
        kinds = self._request_metric["eventKinds"]
        result = event.get("result")
        if isinstance(result, dict):
            for key in ("statusUpdate", "artifactUpdate", "task", "message"):
                if isinstance(result.get(key), dict):
                    kinds[key] = kinds.get(key, 0) + 1
        iac_code = _iac_code_metadata(event)
        if iac_code is None:
            return
        input_value = iac_code.get("input")
        if isinstance(input_value, dict) and isinstance(input_value.get("kind"), str):
            key = "input:" + input_value["kind"]
            kinds[key] = kinds.get(key, 0) + 1
        for envelope in _pipeline_envelopes(iac_code):
            event_type = envelope.get("eventType")
            if isinstance(event_type, str) and event_type:
                key = "pipeline:" + event_type
                kinds[key] = kinds.get(key, 0) + 1

    def _json_error(self, error: StartChatRequestError) -> None:
        body = json.dumps(
            {"Code": error.code, "Message": error.message, "RequestId": str(uuid.uuid4())},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, _format: str, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local HTTPS ROS chat E2E relay.")
    parser.add_argument("--a2a-url", required=True)
    parser.add_argument("--pipeline-a2a-url")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cert-file", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--upstream-timeout", type=float, default=900.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--metrics-file")
    args = parser.parse_args(argv)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(args.cert_file, args.key_file)
    server = StartChatRelay(
        (args.host, args.port),
        a2a_url=args.a2a_url,
        pipeline_a2a_url=args.pipeline_a2a_url,
        workspace=args.workspace,
        ssl_context=ssl_context,
        upstream_timeout=args.upstream_timeout,
        heartbeat_interval=args.heartbeat_interval,
        metrics_path=args.metrics_file,
    )

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(
        json.dumps(
            {"host": args.host, "port": server.server_address[1], "protocol": "https"},
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server._write_metrics()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
