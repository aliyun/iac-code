from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "debugger.py"


def load_debugger_module():
    spec = importlib.util.spec_from_file_location("a2a_debugger", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JsonTargetHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body: dict[str, Any] = {"ok": True}
    response_headers: dict[str, str] = {"Content-Type": "application/json"}
    requests: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        self._record_request()
        self._send_response()

    def do_POST(self) -> None:
        self._record_request()
        self._send_response()

    def _record_request(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        body = raw_body.decode("utf-8") if raw_body else ""
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )

    def _send_response(self) -> None:
        body = json.dumps(self.__class__.response_body).encode("utf-8")
        self.send_response(self.__class__.response_status)
        for name, value in self.__class__.response_headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def serve_handler(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def reset_json_target(*, status: int = 200, body: dict[str, Any] | None = None) -> None:
    JsonTargetHandler.response_status = status
    JsonTargetHandler.response_body = {"ok": True} if body is None else body
    JsonTargetHandler.response_headers = {"Content-Type": "application/json"}
    JsonTargetHandler.requests = []


def start_debugger_server(debugger, *, default_cwd: str = "/workspace/demo"):
    config = debugger.DebuggerConfig(
        host="127.0.0.1",
        port=0,
        default_server_url="http://127.0.0.1:41299",
        default_cwd=default_cwd,
    )
    server = debugger.create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    class RunningServer:
        url = f"http://{host}:{port}"

        def close(self) -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return RunningServer()


def start_logged_debugger_server(debugger, *, log_dir: Path, default_cwd: str = "/workspace/demo"):
    config = debugger.DebuggerConfig(
        host="127.0.0.1",
        port=0,
        default_server_url="http://127.0.0.1:41299",
        default_cwd=default_cwd,
        log_dir=str(log_dir),
    )
    server = debugger.create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address

    class RunningServer:
        url = f"http://{host}:{port}"

        def close(self) -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    return RunningServer()


def get_json(url: str) -> tuple[int, dict[str, Any]]:
    with urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    encoded = json.dumps(body).encode("utf-8")
    request = Request(url, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post_raw(url: str, body: dict[str, Any]) -> tuple[int, str]:
    encoded = json.dumps(body).encode("utf-8")
    request = Request(url, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_parse_args_defaults_to_loopback_and_current_directory(monkeypatch, tmp_path: Path) -> None:
    debugger = load_debugger_module()
    monkeypatch.chdir(tmp_path)

    args = debugger.parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 41880
    assert args.default_server_url == "http://127.0.0.1:41299"
    assert args.default_cwd == str(tmp_path)


def test_script_help_exits_successfully() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Run a local A2A pipeline debugger." in result.stdout


def test_normalize_server_url_accepts_http_and_strips_trailing_slash() -> None:
    debugger = load_debugger_module()

    assert debugger.normalize_server_url(" http://127.0.0.1:41299/ ") == "http://127.0.0.1:41299"
    assert debugger.normalize_server_url("https://example.test/a2a/") == "https://example.test/a2a"


@pytest.mark.parametrize("value", ["", "ftp://example.test", "file:///tmp/a2a", "127.0.0.1:41299"])
def test_normalize_server_url_rejects_invalid_values(value: str) -> None:
    debugger = load_debugger_module()

    with pytest.raises(ValueError, match="serverUrl must be an http or https URL"):
        debugger.normalize_server_url(value)


def test_build_message_stream_payload_uses_a2a_v1_method_and_cwd_metadata() -> None:
    debugger = load_debugger_module()

    payload = debugger.build_message_stream_payload(
        cwd="/workspace/demo",
        prompt="帮我生成售卖 pipeline 方案",
        context_id="ctx-demo",
        task_id="task-demo",
        request_id="req-1",
        message_id="msg-1",
    )

    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "req-1"
    assert payload["method"] == "SendStreamingMessage"
    message = payload["params"]["message"]
    assert message["messageId"] == "msg-1"
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": "帮我生成售卖 pipeline 方案"}]
    assert message["metadata"] == {"iac_code": {"cwd": "/workspace/demo"}}
    assert message["contextId"] == "ctx-demo"
    assert message["taskId"] == "task-demo"
    assert payload["params"]["configuration"] == {"acceptedOutputModes": ["text/plain"]}


def test_build_message_stream_payload_omits_blank_context_id() -> None:
    debugger = load_debugger_module()

    payload = debugger.build_message_stream_payload(
        cwd="/workspace/demo",
        prompt="hello",
        context_id="",
        task_id="",
        request_id="req-1",
        message_id="msg-1",
    )

    assert "contextId" not in payload["params"]["message"]
    assert "taskId" not in payload["params"]["message"]


def test_build_task_payloads_use_a2a_v1_method_names() -> None:
    debugger = load_debugger_module()

    assert debugger.build_task_cancel_payload(task_id="task-1", request_id="req-cancel") == {
        "jsonrpc": "2.0",
        "id": "req-cancel",
        "method": "CancelTask",
        "params": {"id": "task-1"},
    }
    assert debugger.build_task_get_payload(task_id="task-1", history_length=3, request_id="req-get") == {
        "jsonrpc": "2.0",
        "id": "req-get",
        "method": "GetTask",
        "params": {"id": "task-1", "historyLength": 3},
    }


def test_fetch_json_get_returns_status_headers_and_decoded_body() -> None:
    debugger = load_debugger_module()
    reset_json_target(body={"status": "healthy"})

    with serve_handler(JsonTargetHandler) as url:
        result = debugger.fetch_json(f"{url}/health")

    assert result.status_code == 200
    assert result.data == {"status": "healthy"}
    assert result.error is None
    assert JsonTargetHandler.requests[0]["method"] == "GET"
    assert JsonTargetHandler.requests[0]["path"] == "/health"


def test_fetch_json_post_sends_a2a_headers_and_json_body() -> None:
    debugger = load_debugger_module()
    reset_json_target(body={"jsonrpc": "2.0", "result": {"ok": True}})

    with serve_handler(JsonTargetHandler) as url:
        result = debugger.fetch_json(
            f"{url}/",
            method="POST",
            payload={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": "task-1"}},
        )

    assert result.status_code == 200
    assert result.data == {"jsonrpc": "2.0", "result": {"ok": True}}
    request = JsonTargetHandler.requests[0]
    assert request["headers"]["A2A-Version"] == "1.0"
    assert json.loads(request["body"])["method"] == "GetTask"


def test_fetch_json_reports_http_error_with_decoded_body() -> None:
    debugger = load_debugger_module()
    reset_json_target(status=401, body={"error": "Unauthorized"})

    with serve_handler(JsonTargetHandler) as url:
        result = debugger.fetch_json(f"{url}/health")

    assert result.status_code == 401
    assert result.data == {"error": "Unauthorized"}
    assert result.error == "HTTP 401"


def test_index_route_serves_html_with_default_values(tmp_path: Path) -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger, default_cwd=str(tmp_path))
    try:
        with urlopen(running.url, timeout=5) as response:
            html = response.read().decode("utf-8")
    finally:
        running.close()

    assert response.status == 200
    assert "iac-code A2A Pipeline Debugger" in html
    assert "http://127.0.0.1:41299" in html
    assert str(tmp_path) in html


def test_index_html_escapes_defaults_json_for_script_context() -> None:
    debugger = load_debugger_module()
    dangerous_cwd = "</script><script>alert(1)</script>"

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd=dangerous_cwd,
        )
    )

    defaults_start = html.index("window.DEBUGGER_DEFAULTS = ")
    state_start = html.index("const state", defaults_start)
    defaults_assignment = html[defaults_start:state_start]

    assert "</script><script>" not in defaults_assignment
    assert "<\\/script><script>alert(1)<\\/script>" in defaults_assignment
    assert 'value="&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in html


def test_index_html_contains_debugger_controls_and_raw_panels(tmp_path: Path) -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd=str(tmp_path),
        )
    )

    for expected in [
        "iac-code A2A Pipeline Debugger",
        'id="server-url"',
        'id="cwd"',
        'id="context-id"',
        'id="task-id"',
        'id="prompt"',
        'id="stream-button"',
        'id="fetch-state-button"',
        'id="cancel-button"',
        'id="export-html-button"',
        "Export HTML",
        "SSE Events",
        "Snapshot",
        "Requests",
        "Debug Log",
        'id="debug-log-dir"',
    ]:
        assert expected in html


def test_index_html_embedded_script_is_valid_javascript(tmp_path: Path) -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )
    script = html[html.index("<script>") + len("<script>") : html.rindex("</script>")]
    script_path = tmp_path / "debugger-ui.js"
    script_path.write_text(script, encoding="utf-8")

    try:
        result = subprocess.run(["node", "--check", str(script_path)], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        pytest.skip("node is not installed")

    assert result.returncode == 0, result.stderr


def test_index_html_escapes_js_text_newlines() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )
    script = html[html.index("<script>") + len("<script>") : html.rindex("</script>")]

    assert r'`${title}: ${status}\n  ${steps.join("\n  ")}`' in script
    assert r'candidates.map(candidateSummary).join("\n")' in script
    assert r'stage.candidates.map(candidateSummary).join("\n")' in script


def test_index_html_defines_pipeline_metadata_reducer_functions() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function extractPipelineEnvelope",
        "function applyPipelineEvent",
        "function rebuildFromSnapshot",
        "function renderPipeline",
        "function appendRawEvent",
        ".textContent =",
    ]:
        assert expected in html
    assert ".innerHTML =" not in html


def test_index_html_renders_sse_events_as_expandable_rows() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'class="raw-output"',
        "function renderSseEvents",
        "function rawEventLabel",
        'document.createElement("details")',
        'document.createElement("summary")',
        "raw-event-kind",
        "raw-event-meta",
    ]:
        assert expected in html
    assert ".innerHTML =" not in html


def test_index_html_renders_snapshot_as_readable_state_view() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function renderSnapshotState",
        "function renderSnapshotPipelineTree",
        "function renderSnapshotDisplaySections",
        "function renderSnapshotControlSections",
        "function snapshotEnvelope",
        "function onStateSectionToggle",
        "expandedStateSectionKeys",
        "collapsedStateSectionKeys",
        'document.createElement("details")',
        "data-state-section-key",
        "state-summary",
        "state-section",
        "state-node",
        "Full Snapshot JSON",
        "candidateDetails",
        "toolResults",
        "stack history",
        'rawContainer.className = state.activeRawTab === "snapshot"',
    ]:
        assert expected in html
    assert "appendRawJson(rawContainer, state.raw.snapshot)" not in html
    assert ".innerHTML =" not in html


def test_index_html_renders_requests_as_expandable_rows_with_statuses() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function renderRequestEvents",
        "function requestEventLabel",
        "function requestEventSummary",
        "function requestEventMeta",
        "function updateRawRequest",
        "renderRequestEvents(rawContainer, state.raw.requests)",
        'row.status === "error"',
        "prompt is empty",
    ]:
        assert expected in html
    assert ".innerHTML =" not in html


def test_index_html_stream_message_surfaces_non_ok_response_errors() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "async function readResponseError",
        "function errorMessage",
        "if (!response.ok)",
        "updateRawRequest(requestRow",
        'appendRawEvent("sse", {type: "error"',
        "errorMessage(errorBody)",
    ]:
        assert expected in html


def test_index_html_groups_text_delta_events() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function groupSseEvents",
        "function flushTextDeltaGroup",
        'eventType === "text_delta"',
        '"text_delta_group"',
        "currentGroup.count += 1",
        "currentGroup.text += text",
    ]:
        assert expected in html


def test_index_html_surfaces_permission_guidance() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "latestPermission",
        "function permissionGuidance",
        "Permission denied by A2A server",
        "auto_approve_permissions: true",
        "envelope.permission",
        "display.permissions",
    ]:
        assert expected in html


def test_index_html_summarizes_builtin_debugger_events() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'value.type === "health"',
        "value.body.health.status",
        "value.body.agentCard.name",
        'value.type === "cancel"',
        'value.type === "error"',
    ]:
        assert expected in html


def test_index_html_extracts_pipeline_metadata_from_status_update_events() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "payload.statusUpdate",
        "payload.status_update",
        "payload.status",
        "payload.event",
        "payload.events",
    ]:
        assert expected in html


def test_index_html_labels_a2a_task_and_status_payloads() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function a2aTaskIdentity",
        'return "task_submitted"',
        'return "status_update"',
        "statusUpdate.taskId",
        "task.contextId",
        "captureA2ATaskIdentity(payload)",
    ]:
        assert expected in html


def test_index_html_stream_parser_accepts_crlf_sse_frames() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )
    script = html[html.index("<script>") + len("<script>") : html.rindex("</script>")]

    normalize_index = script.index(r'buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");')
    split_index = script.index(r'const chunks = buffer.split("\n\n");')

    assert normalize_index < split_index


def test_index_html_defines_current_pipeline_coordinate_reducers() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function stageFromStep",
        "function upsertCandidate",
        "Array.isArray(envelope.steps)",
        'envelope.step && typeof envelope.step === "object"',
        'envelope.candidate && typeof envelope.candidate === "object"',
        'envelope.candidateStep && typeof envelope.candidateStep === "object"',
    ]:
        assert expected in html


def test_index_html_defines_incremental_execution_tree_renderer() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "executionTree",
        "expandedTreeKeys",
        "function appendExecutionTreeEvent",
        "function renderExecutionTreePatch",
        "function renderTreeNode",
        "function onTreeToggle",
        "data-tree-key",
        "tree-node-timeline",
    ]:
        assert expected in html
    assert 'byId("structured-pipeline").textContent = ""' not in html


def test_index_html_models_parallel_candidates_and_rollback_history() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "parallel-group",
        "candidate-lanes",
        "candidate-lane",
        '"rollback_completed"',
        '"rollback_triggered"',
        "appendTimelineItem",
        "summarizeTimelineEvent",
        "tool_result",
        "permission_requested",
        "text_delta",
    ]:
        assert expected in html


def test_index_html_assigns_timeline_events_to_business_nodes() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function timelineTargetKeyFromEnvelope",
        "state.executionTree.lastStepKey",
        "state.executionTree.lastCandidateKey",
        "state.executionTree.lastCandidateStepKey",
        "candidateStepKey || candidateKey || stepKey || pipeline.key",
        'if (["text_delta", "tool_result", "permission_requested", "input_required"].includes(type))',
        "delete state.executionTree.textGroups[textKey]",
    ]:
        assert expected in html


def test_index_html_renders_timeline_details_with_pretty_json() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function detailTextForTimelineItem",
        "function renderTimelineDetails",
        "timeline-details-button",
        "timeline-detail-body",
        "JSON.stringify(item.raw, null, 2)",
        'button.type = "button"',
    ]:
        assert expected in html


def test_index_html_sorts_execution_tree_children_by_coordinate_order() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function treeNodeOrder",
        "function sortedTreeChildKeys",
        "sortOrder: treeCoordinateOrder(envelope.step)",
        "sortedTreeChildKeys(node.children).forEach",
        "sortedTreeChildKeys(state.executionTree.rootIds).forEach",
    ]:
        assert expected in html


def test_index_html_timeline_details_button_does_not_toggle_parent_details() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    assert 'button.addEventListener("click", (event) => {' in html
    assert "event.preventDefault();" in html
    assert "event.stopPropagation();" in html
    assert 'detail.addEventListener("toggle", stopNestedTimelineToggle);' in html


def test_index_html_groups_normal_a2a_message_deltas_outside_pipeline_steps() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function rawItemValue",
        "function a2aStatusMessage",
        "function normalMessageGroupKey",
        "function appendNormalMessageEvent",
        "function ensureNormalChatNode",
        '"a2a_message_group"',
        '"agent_message_delta"',
        '"Normal Chat"',
        'if (!envelope || typeof envelope !== "object") {',
        "appendNormalMessageEvent(row);",
    ]:
        assert expected in html


def test_index_html_deduplicates_pipeline_events_across_overlapping_streams() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "pipelineEventDedupKey",
        "rememberPipelineEvent",
        "resetPipelineEventDedup",
        "dedupeRawSseEvents",
        "rawPipelineEventKeys: new Set()",
        "applyPipelineEvent(event)",
        "applyPipelineEvent(parsed, rawRow, {alreadyRecorded: true})",
        "if (!options.alreadyRecorded && !rememberPipelineEvent(payload))",
        "if (dedupKey && state.rawPipelineEventKeys.has(dedupKey))",
        "state.raw.sse = dedupeRawSseEvents(restoredSseEvents);",
        "resetPipelineEventDedup();",
    ]:
        assert expected in html


def test_index_html_binds_tree_toggle_handlers_to_restored_export_details() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function ensureTreeToggleListener",
        'details.getAttribute("data-tree-toggle-bound") === "true"',
        'details.setAttribute("data-tree-toggle-bound", "true");',
        "ensureTreeToggleListener(details);",
    ]:
        assert expected in html


def test_index_html_uses_readable_candidate_layout() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        ".candidate-lanes",
        "grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));",
        ".candidate-lane > .tree-summary",
        ".candidate-lane .tree-meta",
        "overflow-wrap: anywhere;",
    ]:
        assert expected in html


def test_index_html_routes_interrupt_events_to_parent_step_not_candidate_lane() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'type.startsWith("interrupt_") || envelope.scope === "interrupt"',
        "return stepKey || pipeline.key;",
    ]:
        assert expected in html


def test_index_html_keeps_ask_user_question_input_received_step_working() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    assert 'if (eventType === "input_received" && scope === "step")' in html
    assert 'eventData(envelope).kind === "ask_user_question" ? "working" : "completed"' in html


def test_index_html_uses_captured_context_and_task_ids_for_requests() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    assert "controls.contextId || state.contextId" in html
    assert "contextId: controls.contextId || state.contextId," in html
    assert "taskId: streamTaskIdForControls(controls)," in html


def test_index_html_distinguishes_pipeline_and_active_task_ids() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'label for="task-id">Pipeline taskId',
        'label for="active-task-id">Active taskId',
        'id="metric-pipeline-task"',
        'id="metric-active-task"',
        'id="task-history"',
        "activeTaskId",
        "function recordTaskIdentity",
        "function renderTaskHistory",
    ]:
        assert expected in html


def test_index_html_cancel_uses_active_task_id() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function cancelTaskIdForControls",
        "const taskId = cancelTaskIdForControls(controls);",
        "controls.activeTaskId || state.activeTaskId",
        'appendRawEvent("sse", {type: "cancel_error", error: "No active task to cancel."})',
    ]:
        assert expected in html


def test_index_html_yields_between_batched_sse_events() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function nextBrowserPaint",
        "async function yieldToBrowserAfterStreamEvent",
        "for (const chunk of chunks)",
        "await yieldToBrowserAfterStreamEvent(parsed, ++streamEventCount);",
        'eventType !== "text_delta" || streamEventCount % 8 === 0',
    ]:
        assert expected in html
    assert "chunks.forEach((chunk) => {" not in html


def test_index_html_omits_completed_pipeline_task_id_after_normal_handoff() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "normalHandoffReady",
        "function streamTaskIdForControls",
        "state.normalHandoffReady && !controls.activeTaskId",
        'state.activeTaskId = "";',
        'return "";',
        "updateNormalHandoffState(envelope);",
    ]:
        assert expected in html


def test_index_html_clears_finished_active_task_after_normal_chat_turn() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function isWorkingA2ATaskState",
        "function shouldKeepActiveTaskId",
        "if (taskId && shouldKeepActiveTaskId(identity))",
        "else if (taskId && state.activeTaskId === taskId)",
        'state.activeTaskId = "";',
    ]:
        assert expected in html


def test_index_html_exports_readonly_debugger_clone() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function buildExportSnapshot",
        "function buildExportHtml",
        "function exportCurrentHtmlSnapshot",
        "function restoreExportState",
        "function restoreExecutionTree",
        "function configureExportMode",
        "function cloneDocumentForExport",
        "window.DEBUGGER_EXPORT_DATA",
        "document.documentElement.cloneNode(true)",
        "data-export-mode",
        "Blob([html]",
        '"sseEvents"',
        '"executionTree"',
        "expandedTimelineKeys:",
        '"expandedRawEventKeys"',
        'byId("export-html-button").addEventListener',
    ]:
        assert expected in html
    assert "function renderExportTree" not in html
    assert "function renderExportTimelineItem" not in html
    assert "function renderExportJsonOnDemand" not in html
    assert "<pre>${escapeHtml(json)}</pre>" not in html
    assert '<pre>${escapeHtml(JSON.stringify(data.snapshot, null, 2) || "null")}</pre>' not in html


def test_index_html_can_restore_debugger_log_replay_payload(tmp_path: Path) -> None:
    debugger = load_debugger_module()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "sse-events.jsonl").write_text(
        "\n".join(
            [
                (
                    '{"raw":{"metadata":{"iac_code":{"pipeline":'
                    '{"eventType":"pipeline_started","sequence":1,"taskId":"task-pipeline","contextId":"ctx-1"}'
                    "}}}}"
                ),
                ('{"raw":{"statusUpdate":{"taskId":"task-active","contextId":"ctx-1","status":{"state":"working"}}}}'),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (log_dir / "snapshots.jsonl").write_text('{"raw":{"ok":true,"state":{"status":"working"}}}\n', encoding="utf-8")
    replay = debugger.load_debug_log_export(log_dir)

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
            replay_export=replay,
        )
    )

    assert "window.DEBUGGER_REPLAY_DATA" in html
    assert "rebuildExecutionTreeFromRawEvents" in html
    assert (
        '"sseEvents": [{"metadata": {"iac_code": {"pipeline": '
        '{"eventType": "pipeline_started", "sequence": 1, "taskId": "task-pipeline", "contextId": "ctx-1"}}}}, '
        '{"statusUpdate": {"taskId": "task-active", "contextId": "ctx-1", "status": {"state": "working"}}}]' in html
    )
    assert replay["task"]["taskId"] == "task-pipeline"
    assert replay["task"]["activeTaskId"] == "task-active"
    assert {"taskId": "task-pipeline", "contextId": "ctx-1", "state": "", "role": "pipeline"} in replay["taskHistory"]
    assert {"taskId": "task-active", "contextId": "ctx-1", "state": "working", "role": "active"} in replay[
        "taskHistory"
    ]


def test_index_html_fills_context_and_task_id_controls_after_capture() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function syncCapturedIdentityControls",
        'const contextInput = byId("context-id")',
        'const taskInput = byId("task-id")',
        "syncCapturedIdentityControls();",
    ]:
        assert expected in html


def test_index_html_preserves_raw_event_expansion_across_renders() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "expandedRawEventKeys",
        "function rawRowKey",
        "function onRawEventToggle",
        "data-raw-key",
        "state.expandedRawEventKeys.has",
    ]:
        assert expected in html


def test_index_html_reads_input_required_data_and_clears_stale_permissions() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'envelope.eventType === "input_required"',
        "envelope.data",
        "state.latestPermission = null",
        "fetchStateIfAvailable",
    ]:
        assert expected in html


def test_index_html_stops_stream_after_input_required_to_reenable_prompt_submit() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function shouldStopStreamingAfterPayload",
        'eventTypeOf(envelope) === "input_required"',
        '"TASK_STATE_INPUT_REQUIRED"',
        "let shouldStopStream = false;",
        "shouldStopStream = true;",
        "await cancelReaderSafely(reader);",
    ]:
        assert expected in html


def test_index_html_keeps_reading_after_terminal_task_status_for_pipeline_sidecars() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    should_stop_body = html.split("function shouldStopStreamingAfterPayload", 1)[1].split("function rawItemValue", 1)[0]
    assert "TASK_STATE_INPUT_REQUIRED" in should_stop_body
    assert "TASK_STATE_COMPLETED" not in should_stop_body
    assert "TASK_STATE_FAILED" not in should_stop_body
    assert "TASK_STATE_CANCELED" not in should_stop_body


def test_index_html_allows_stream_button_for_running_pipeline_interrupts() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "streamsInFlight: 0",
        "function withStreamAction",
        'byId("stream-button").addEventListener("click", '
        "(event) => withStreamAction(event.currentTarget, streamMessage));",
        "state.streamsInFlight += 1;",
        "state.streamsInFlight = Math.max(0, state.streamsInFlight - 1);",
        'if (state.streamsInFlight === 1 && !state.waitingInput && state.status !== "error")',
    ]:
        assert expected in html
    blocking_stream_binding = (
        'byId("stream-button").addEventListener("click", '
        "(event) => withButtonState(event.currentTarget, streamMessage));"
    )
    assert blocking_stream_binding not in html


def test_index_html_maps_live_event_types_to_coordinate_statuses() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "function statusFromEventType",
        '"step_completed": "completed"',
        '"step_failed": "failed"',
        '"input_required": "waiting_input"',
        '"candidate_step_completed": "completed"',
        '"candidate_step_failed": "failed"',
        'const stepStatus = statusFromEventType(envelope.eventType, envelope.step.status, "step", envelope)',
        "const candidateStatus = statusFromEventType(",
        "const candidateStepStatus = statusFromEventType(",
        "envelope.candidateStep.status,",
        '"candidateStep"',
        "envelope",
    ]:
        assert expected in html


def test_index_html_reads_current_waiting_input_fields() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    assert "pendingInput" in html
    assert "envelope.input" in html


def test_index_html_displays_snapshot_interaction_histories() -> None:
    debugger = load_debugger_module()

    html = debugger.render_index_html(
        debugger.DebuggerConfig(
            host="127.0.0.1",
            port=41880,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        "input history",
        "interrupt history",
        "handoff history",
        "control.inputHistory",
        "control.interruptHistory",
        "control.handoffHistory",
    ]:
        assert expected in html


def test_health_route_proxies_health_and_agent_card() -> None:
    debugger = load_debugger_module()

    class HealthTargetHandler(BaseHTTPRequestHandler):
        requests: list[str] = []

        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            self.__class__.requests.append(self.path)
            if self.path == "/health":
                body = {"status": "healthy"}
            elif self.path == "/.well-known/agent-card.json":
                body = {"name": "iac-code"}
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with serve_handler(HealthTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            status, body = get_json(f"{running.url}/api/health?{urlencode({'serverUrl': target_url})}")
        finally:
            running.close()

    assert status == 200
    assert body == {
        "ok": True,
        "health": {"status": "healthy"},
        "agentCard": {"name": "iac-code"},
    }
    assert HealthTargetHandler.requests == ["/health", "/.well-known/agent-card.json"]


def test_health_route_writes_debugger_jsonl_logs(tmp_path: Path) -> None:
    debugger = load_debugger_module()

    class HealthTargetHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            if self.path == "/health":
                body = {"status": "healthy"}
            elif self.path == "/.well-known/agent-card.json":
                body = {"name": "iac-code"}
            else:
                self.send_response(404)
                self.end_headers()
                return
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with serve_handler(HealthTargetHandler) as target_url:
        running = start_logged_debugger_server(debugger, log_dir=tmp_path)
        try:
            status, body = get_json(f"{running.url}/api/health?{urlencode({'serverUrl': target_url})}")
        finally:
            running.close()

    assert status == 200
    assert body["ok"] is True
    request_records = read_jsonl(tmp_path / "requests.jsonl")
    sse_records = read_jsonl(tmp_path / "sse-events.jsonl")
    assert request_records[0]["kind"] == "request"
    assert request_records[0]["raw"]["path"].startswith("/api/health?")
    assert sse_records[0]["kind"] == "sse"
    assert sse_records[0]["parsedEventType"] == "health"
    assert sse_records[0]["raw"]["health"] == {"status": "healthy"}


def test_invalid_server_url_returns_bad_request() -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        with pytest.raises(HTTPError) as exc_info:
            get_json(f"{running.url}/api/health?serverUrl=ftp%3A%2F%2Fexample.test")
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {
        "ok": False,
        "error": "serverUrl must be an http or https URL",
    }


def test_health_route_rejects_upstream_200_non_json_response() -> None:
    debugger = load_debugger_module()

    class TextTargetHandler(BaseHTTPRequestHandler):
        requests: list[str] = []

        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            self.__class__.requests.append(self.path)
            encoded = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with serve_handler(TextTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            with pytest.raises(HTTPError) as exc_info:
                urlopen(f"{running.url}/api/health?{urlencode({'serverUrl': target_url})}", timeout=5)
        finally:
            running.close()

    assert exc_info.value.code == 502
    assert json.loads(exc_info.value.read().decode("utf-8")) == {
        "ok": False,
        "statusCode": 200,
        "error": "Target server returned a non-JSON response",
        "body": "<html>not json</html>",
    }
    assert TextTargetHandler.requests == ["/health"]


def test_pipeline_state_route_requires_context_or_task() -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{running.url}/api/pipeline/state?serverUrl=http%3A%2F%2F127.0.0.1%3A41299", timeout=5)
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {
        "ok": False,
        "error": "contextId or taskId is required",
    }


def test_pipeline_state_route_proxies_query_parameters() -> None:
    debugger = load_debugger_module()
    reset_json_target(body={"snapshot": {"lastSequence": 3}, "events": []})

    with serve_handler(JsonTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            query = urlencode(
                {
                    "serverUrl": target_url,
                    "contextId": "ctx-1",
                    "taskId": "task-1",
                    "afterSequence": "2",
                }
            )
            status, body = get_json(f"{running.url}/api/pipeline/state?{query}")
        finally:
            running.close()

    assert status == 200
    assert body == {"snapshot": {"lastSequence": 3}, "events": []}
    assert JsonTargetHandler.requests[0]["path"] == (
        "/iac-code/pipeline/state?contextId=ctx-1&taskId=task-1&afterSequence=2"
    )


def test_task_get_route_sends_get_task_jsonrpc() -> None:
    debugger = load_debugger_module()
    reset_json_target(body={"jsonrpc": "2.0", "result": {"id": "task-1"}})

    with serve_handler(JsonTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            query = urlencode({"serverUrl": target_url, "taskId": "task-1", "historyLength": "5"})
            status, body = get_json(f"{running.url}/api/task/get?{query}")
        finally:
            running.close()

    assert status == 200
    assert body == {"jsonrpc": "2.0", "result": {"id": "task-1"}}
    sent = json.loads(JsonTargetHandler.requests[0]["body"])
    assert JsonTargetHandler.requests[0]["path"] == "/"
    assert sent["method"] == "GetTask"
    assert sent["params"] == {"id": "task-1", "historyLength": 5}


@pytest.mark.parametrize("query", ["", urlencode({"serverUrl": ""})])
def test_task_get_route_requires_task_id_before_server_url(query: str) -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        separator = "?" if query else ""
        with pytest.raises(HTTPError) as exc_info:
            get_json(f"{running.url}/api/task/get{separator}{query}")
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {"ok": False, "error": "taskId is required"}


def test_task_get_route_rejects_non_integer_history_length() -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        query = urlencode({"serverUrl": "http://127.0.0.1:41299", "taskId": "task-1", "historyLength": "abc"})
        with pytest.raises(HTTPError) as exc_info:
            get_json(f"{running.url}/api/task/get?{query}")
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {
        "ok": False,
        "error": "historyLength must be an integer",
    }


def test_task_get_route_rejects_negative_history_length() -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        query = urlencode({"serverUrl": "http://127.0.0.1:41299", "taskId": "task-1", "historyLength": "-1"})
        with pytest.raises(HTTPError) as exc_info:
            get_json(f"{running.url}/api/task/get?{query}")
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {
        "ok": False,
        "error": "historyLength must be greater than or equal to 0",
    }


class SseTargetHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        self.__class__.requests.append(
            {
                "headers": dict(self.headers.items()),
                "body": raw_body.decode("utf-8"),
            }
        )
        events = [
            {
                "jsonrpc": "2.0",
                "result": {
                    "task": {
                        "id": "task-1",
                        "contextId": "ctx-1",
                        "status": {"state": "TASK_STATE_SUBMITTED"},
                    }
                },
            },
            {
                "jsonrpc": "2.0",
                "result": {
                    "statusUpdate": {
                        "taskId": "task-1",
                        "contextId": "ctx-1",
                        "status": {"state": "TASK_STATE_WORKING"},
                        "metadata": {
                            "iac_code": {
                                "pipeline": {
                                    "eventType": "pipeline_started",
                                    "sequence": 1,
                                    "eventId": "evt-1",
                                    "taskId": "task-1",
                                    "contextId": "ctx-1",
                                }
                            }
                        },
                    }
                },
            },
        ]
        body = b"".join(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n" for event in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EmptySseTargetHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:
        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        assert raw_body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_message_stream_route_forwards_sse_and_uses_stream_payload() -> None:
    debugger = load_debugger_module()
    SseTargetHandler.requests = []

    with serve_handler(SseTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            status, body = post_raw(
                f"{running.url}/api/message/stream",
                {
                    "serverUrl": target_url,
                    "cwd": "/workspace/demo",
                    "contextId": "ctx-1",
                    "taskId": "task-1",
                    "prompt": "start pipeline",
                },
            )
        finally:
            running.close()

    assert status == 200
    assert "data: " in body
    assert "pipeline_started" in body
    sent = json.loads(SseTargetHandler.requests[0]["body"])
    assert SseTargetHandler.requests[0]["headers"]["A2A-Version"] == "1.0"
    assert sent["method"] == "SendStreamingMessage"
    assert sent["params"]["message"]["contextId"] == "ctx-1"
    assert sent["params"]["message"]["taskId"] == "task-1"
    assert sent["params"]["message"]["metadata"] == {"iac_code": {"cwd": "/workspace/demo"}}


def test_message_stream_route_writes_sse_debugger_log(tmp_path: Path) -> None:
    debugger = load_debugger_module()
    SseTargetHandler.requests = []

    with serve_handler(SseTargetHandler) as target_url:
        running = start_logged_debugger_server(debugger, log_dir=tmp_path)
        try:
            status, body = post_raw(
                f"{running.url}/api/message/stream",
                {
                    "serverUrl": target_url,
                    "cwd": "/workspace/demo",
                    "contextId": "ctx-1",
                    "prompt": "start pipeline",
                },
            )
        finally:
            running.close()

    assert status == 200
    assert "pipeline_started" in body
    records = read_jsonl(tmp_path / "sse-events.jsonl")
    assert records[0]["parsedEventType"] == "task_submitted"
    assert records[0]["taskId"] == "task-1"
    assert records[0]["contextId"] == "ctx-1"
    assert records[1]["parsedEventType"] == "pipeline_started"
    assert records[1]["taskId"] == "task-1"
    assert records[1]["contextId"] == "ctx-1"
    assert records[1]["sequence"] == 1
    assert records[1]["raw"]["result"]["statusUpdate"]["metadata"]["iac_code"]["pipeline"]["eventId"] == "evt-1"


def test_message_stream_route_logs_empty_upstream_stream(tmp_path: Path) -> None:
    debugger = load_debugger_module()

    with serve_handler(EmptySseTargetHandler) as target_url:
        running = start_logged_debugger_server(debugger, log_dir=tmp_path)
        try:
            status, body = post_raw(
                f"{running.url}/api/message/stream",
                {
                    "serverUrl": target_url,
                    "cwd": "/workspace/demo",
                    "contextId": "ctx-1",
                    "prompt": "follow up",
                },
            )
        finally:
            running.close()

    assert status == 200
    assert body == ""
    records = read_jsonl(tmp_path / "sse-events.jsonl")
    assert records[-1]["parsedEventType"] == "stream_empty"
    assert records[-1]["raw"] == {"type": "stream_empty", "statusCode": 200}


def test_message_stream_route_ignores_client_disconnect_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    debugger = load_debugger_module()

    class SlowSseTargetHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for index in range(50):
                try:
                    self.wfile.write(f'data: {{"index": {index}}}\n\n'.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                time.sleep(0.02)

    with serve_handler(SlowSseTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            encoded = json.dumps(
                {
                    "serverUrl": target_url,
                    "cwd": "/workspace/demo",
                    "prompt": "start pipeline",
                }
            ).encode("utf-8")
            request = Request(
                f"{running.url}/api/message/stream",
                data=encoded,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            response = urlopen(request, timeout=5)
            try:
                assert response.status == 200
                assert response.readline().startswith(b"data: ")
            finally:
                response.close()
            time.sleep(0.3)
        finally:
            running.close()

    captured = capsys.readouterr()
    assert "BrokenPipeError" not in captured.err
    assert "Exception occurred during processing" not in captured.err


def test_message_stream_route_requires_cwd_and_prompt() -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        with pytest.raises(HTTPError) as exc_info:
            post_json(
                f"{running.url}/api/message/stream",
                {
                    "serverUrl": "http://127.0.0.1:41299",
                    "cwd": "",
                    "prompt": "",
                },
            )
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert exc_info.value.headers["Content-Type"] == "application/json; charset=utf-8"
    body = json.loads(exc_info.value.read().decode("utf-8"))
    assert body["ok"] is False
    assert body["error"] == "cwd is required"


def test_task_cancel_route_sends_cancel_task_jsonrpc() -> None:
    debugger = load_debugger_module()
    reset_json_target(body={"jsonrpc": "2.0", "result": {"id": "task-1", "status": "canceled"}})

    with serve_handler(JsonTargetHandler) as target_url:
        running = start_debugger_server(debugger)
        try:
            status, body = post_json(f"{running.url}/api/task/cancel", {"serverUrl": target_url, "taskId": "task-1"})
        finally:
            running.close()

    assert status == 200
    assert body == {"jsonrpc": "2.0", "result": {"id": "task-1", "status": "canceled"}}
    sent = json.loads(JsonTargetHandler.requests[0]["body"])
    assert sent["method"] == "CancelTask"
    assert sent["params"] == {"id": "task-1"}


@pytest.mark.parametrize("body", [{}, {"serverUrl": ""}])
def test_task_cancel_route_requires_task_id_before_server_url(body: dict[str, Any]) -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        with pytest.raises(HTTPError) as exc_info:
            post_json(f"{running.url}/api/task/cancel", body)
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {"ok": False, "error": "taskId is required"}


def test_task_cancel_route_requires_task_id() -> None:
    debugger = load_debugger_module()
    running = start_debugger_server(debugger)
    try:
        with pytest.raises(HTTPError) as exc_info:
            post_json(f"{running.url}/api/task/cancel", {"serverUrl": "http://127.0.0.1:41299"})
    finally:
        running.close()

    assert exc_info.value.code == 400
    assert json.loads(exc_info.value.read().decode("utf-8")) == {"ok": False, "error": "taskId is required"}
