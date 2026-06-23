# Selling Pipeline Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local `scripts/` website that turns A2A selling pipeline streams into an Alibaba Cloud-style purchase-plan console.

**Architecture:** Add a self-contained Python HTTP server at `scripts/a2a/selling_console.py` that reuses `scripts/a2a/debugger.py` protocol helpers for A2A proxying. Serve static HTML/CSS/JS from `scripts/a2a/selling_console_web/`; the frontend owns the selling-specific reducer, visual shell, candidate selection, pending input, and normal handoff behavior.

**Tech Stack:** Python stdlib `http.server`, pytest, browser-native HTML/CSS/JavaScript, existing `uv`/Makefile workflow.

---

## File Structure

- Create `scripts/a2a/selling_console.py`
  - Parse CLI arguments.
  - Render the static shell with safe JSON defaults.
  - Serve static files from `scripts/a2a/selling_console_web/`.
  - Proxy A2A health, streaming, task get, task cancel, and pipeline state through `scripts.a2a.debugger` helpers.
- Create `scripts/a2a/selling_console_web/index.html`
  - Alibaba Cloud-style top navigation.
  - Left assistant workflow panel.
  - Right purchase plan area.
  - Diagnostic drawer container.
- Create `scripts/a2a/selling_console_web/styles.css`
  - Screenshot-matching layout, spacing, colors, cards, responsive behavior.
- Create `scripts/a2a/selling_console_web/app.js`
  - Pure reducer helpers exported under `window.SellingConsoleReducers`.
  - DOM controller for health, stream, state fetch, cancel, candidate selection, pending input, debug drawer.
- Create `tests/a2a/test_selling_console_script.py`
  - Python server, routes, proxy, escaping, static safety, and JavaScript syntax checks.
- Create `tests/a2a/test_selling_console_frontend.py`
  - Node-backed reducer behavior tests. Skip when Node is unavailable.
- Modify `scripts/README.md`
  - Add the new local selling console script to the scripts table and usage commands.

## Task 1: Python Server Contract

**Files:**
- Create: `scripts/a2a/selling_console.py`
- Create: `tests/a2a/test_selling_console_script.py`

- [ ] **Step 1: Write failing server tests**

Create `tests/a2a/test_selling_console_script.py` with:

```python
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "selling_console.py"


def load_module():
    spec = importlib.util.spec_from_file_location("selling_console", SCRIPT_PATH)
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
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": raw_body.decode("utf-8") if raw_body else "",
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
def serve_handler(handler_cls: type[BaseHTTPRequestHandler]):
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


def start_console(console, *, default_cwd: str = "/workspace/demo"):
    config = console.SellingConsoleConfig(
        host="127.0.0.1",
        port=0,
        default_server_url="http://127.0.0.1:41299",
        default_cwd=default_cwd,
    )
    server = console.create_server(config)
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


def get_text(url: str) -> tuple[int, str, str]:
    with urlopen(url, timeout=5) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")


def get_json(url: str) -> tuple[int, dict[str, Any]]:
    with urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def post_json(url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_parse_args_defaults_to_loopback_and_current_directory(monkeypatch, tmp_path: Path) -> None:
    console = load_module()
    monkeypatch.chdir(tmp_path)

    args = console.parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 41980
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
    assert "Run a local A2A selling pipeline console." in result.stdout


def test_index_route_serves_selling_console_html(tmp_path: Path) -> None:
    console = load_module()
    running = start_console(console, default_cwd=str(tmp_path))
    try:
        status, content_type, html = get_text(running.url)
    finally:
        running.close()

    assert status == 200
    assert "text/html" in content_type
    assert "阿里云" in html
    assert "您的购买方案" in html
    assert "window.SELLING_CONSOLE_DEFAULTS" in html
    assert "http://127.0.0.1:41299" in html
    assert str(tmp_path) in html


def test_index_html_escapes_defaults_json_for_script_context() -> None:
    console = load_module()
    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="</script><script>alert(1)</script>",
        )
    )

    defaults_start = html.index("window.SELLING_CONSOLE_DEFAULTS = ")
    script_end = html.index("</script>", defaults_start)
    defaults_assignment = html[defaults_start:script_end]

    assert "</script><script>" not in defaults_assignment
    assert "<\\/script><script>alert(1)<\\/script>" in defaults_assignment


def test_static_asset_route_serves_css_and_js() -> None:
    console = load_module()
    running = start_console(console)
    try:
        css_status, css_type, css = get_text(f"{running.url}/styles.css")
        js_status, js_type, js = get_text(f"{running.url}/app.js")
    finally:
        running.close()

    assert css_status == 200
    assert "text/css" in css_type
    assert ".topbar" in css
    assert js_status == 200
    assert "javascript" in js_type
    assert "SellingConsoleReducers" in js


def test_static_asset_route_rejects_path_traversal() -> None:
    console = load_module()
    running = start_console(console)
    try:
        with pytest.raises(HTTPError) as exc_info:
            get_text(f"{running.url}/../debugger.py")
    finally:
        running.close()

    assert exc_info.value.code == 404


def test_health_route_proxies_a2a_health_and_agent_card() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"status": "ok"}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            query = urlencode({"serverUrl": target})
            status, body = get_json(f"{running.url}/api/health?{query}")
        finally:
            running.close()

    assert status == 200
    assert body["ok"] is True
    assert [request["path"] for request in JsonTargetHandler.requests] == ["/health", "/.well-known/agent-card.json"]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py -v
```

Expected: FAIL because `scripts/a2a/selling_console.py` does not exist.

- [ ] **Step 3: Implement minimal Python server and stub assets**

Create `scripts/a2a/selling_console.py` with the dataclass, parser, HTML renderer, static serving, and proxy route skeleton. Use `scripts.a2a.debugger` helpers instead of copying protocol code.

Key implementation shape:

```python
from __future__ import annotations

import argparse
import html
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from scripts.a2a import debugger as a2a_debugger

WEB_DIR = Path(__file__).with_name("selling_console_web")


@dataclass(frozen=True)
class SellingConsoleConfig:
    host: str
    port: int
    default_server_url: str
    default_cwd: str
    log_dir: str = ""
    replay_export: dict[str, Any] | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local A2A selling pipeline console.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=41980)
    parser.add_argument("--default-server-url", default="http://127.0.0.1:41299")
    parser.add_argument("--default-cwd", default=os.getcwd())
    parser.add_argument("--log-dir", default="")
    parser.add_argument("--load-log-dir", default="")
    return parser.parse_args(argv)


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def render_index_html(config: SellingConsoleConfig) -> str:
    template = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    defaults = {
        "serverUrl": config.default_server_url,
        "cwd": config.default_cwd,
        "debugLogDir": config.log_dir,
    }
    return (
        template.replace("__DEFAULTS_JSON__", _json_for_script(defaults))
        .replace("__DEFAULT_SERVER_URL__", html.escape(config.default_server_url, quote=True))
        .replace("__DEFAULT_CWD__", html.escape(config.default_cwd, quote=True))
    )
```

Also create minimal stub assets:

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>阿里云 Selling Pipeline Console</title>
  <link rel="stylesheet" href="/styles.css">
  <script>window.SELLING_CONSOLE_DEFAULTS = __DEFAULTS_JSON__;</script>
  <script src="/app.js" defer></script>
</head>
<body>
  <header class="topbar"><strong class="brand">阿里云</strong></header>
  <main id="app"><h1>您的购买方案</h1></main>
</body>
</html>
```

```css
.topbar { height: 56px; }
```

```javascript
(function () {
  window.SellingConsoleReducers = {};
})();
```

Implement `create_server()` routes:

```python
def create_server(config: SellingConsoleConfig) -> ThreadingHTTPServer:
    class SellingConsoleHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _send_text(self, render_index_html(config), content_type="text/html")
                return
            if parsed.path == "/api/health":
                status, body = a2a_debugger._health_response(a2a_debugger._query_params(self.path).get("serverUrl", ""))
                _send_json(self, status, body)
                return
            _send_static(self, parsed.path)
    return ThreadingHTTPServer((config.host, config.port), SellingConsoleHandler)
```

- [ ] **Step 4: Run tests and verify GREEN for server basics**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py -v
```

Expected: PASS for parser, help, index, escaping, static serving, and health route.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add scripts/a2a/selling_console.py scripts/a2a/selling_console_web/index.html scripts/a2a/selling_console_web/styles.css scripts/a2a/selling_console_web/app.js tests/a2a/test_selling_console_script.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: add selling pipeline console server"
```

## Task 2: Complete A2A Proxy Routes

**Files:**
- Modify: `scripts/a2a/selling_console.py`
- Modify: `tests/a2a/test_selling_console_script.py`

- [ ] **Step 1: Add failing proxy tests**

Append tests for `/api/pipeline/state`, `/api/task/get`, `/api/task/cancel`, and `/api/message/stream`. Use the same `JsonTargetHandler` plus a new SSE target:

```python
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
        body = b'data: {"jsonrpc":"2.0","result":{"id":"task-1","contextId":"ctx-1","status":{"state":"TASK_STATE_WORKING"}}}\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
```

Add assertions:

```python
def test_pipeline_state_route_requires_context_or_task() -> None:
    console = load_module()
    running = start_console(console)
    try:
        query = urlencode({"serverUrl": "http://127.0.0.1:41299"})
        with pytest.raises(HTTPError) as exc_info:
            get_json(f"{running.url}/api/pipeline/state?{query}")
    finally:
        running.close()

    assert exc_info.value.code == 400


def test_pipeline_state_route_proxies_query_parameters() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"snapshot": {"status": "working"}}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            query = urlencode({"serverUrl": target, "contextId": "ctx-1", "taskId": "task-1", "afterSequence": "7"})
            status, body = get_json(f"{running.url}/api/pipeline/state?{query}")
        finally:
            running.close()

    assert status == 200
    assert body == {"snapshot": {"status": "working"}}
    assert JsonTargetHandler.requests[0]["path"] == "/iac-code/pipeline/state?contextId=ctx-1&taskId=task-1&afterSequence=7"


def test_task_get_route_sends_get_task_jsonrpc() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"jsonrpc": "2.0", "result": {"id": "task-1"}}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            query = urlencode({"serverUrl": target, "taskId": "task-1", "historyLength": "2"})
            status, body = get_json(f"{running.url}/api/task/get?{query}")
        finally:
            running.close()

    assert status == 200
    payload = json.loads(JsonTargetHandler.requests[0]["body"])
    assert payload["method"] == "GetTask"
    assert payload["params"] == {"id": "task-1", "historyLength": 2}


def test_task_cancel_route_sends_cancel_task_jsonrpc() -> None:
    console = load_module()
    JsonTargetHandler.requests = []
    JsonTargetHandler.response_body = {"jsonrpc": "2.0", "result": {"id": "task-1"}}

    with serve_handler(JsonTargetHandler) as target:
        running = start_console(console)
        try:
            status, body = post_json(f"{running.url}/api/task/cancel", {"serverUrl": target, "taskId": "task-1"})
        finally:
            running.close()

    assert status == 200
    payload = json.loads(JsonTargetHandler.requests[0]["body"])
    assert payload["method"] == "CancelTask"
    assert payload["params"] == {"id": "task-1"}


def test_message_stream_route_forwards_sse_and_cwd_metadata() -> None:
    console = load_module()
    SseTargetHandler.requests = []

    with serve_handler(SseTargetHandler) as target:
        running = start_console(console)
        try:
            status, text, content_type = post_raw(
                f"{running.url}/api/message/stream",
                {"serverUrl": target, "cwd": "/workspace/demo", "prompt": "部署一个静态网站"},
            )
        finally:
            running.close()

    assert status == 200
    assert "event-stream" in content_type
    assert "TASK_STATE_WORKING" in text
    payload = json.loads(SseTargetHandler.requests[0]["body"])
    assert payload["method"] == "SendStreamingMessage"
    assert payload["params"]["message"]["metadata"] == {"iac_code": {"cwd": "/workspace/demo"}}
```

Add `post_raw()` helper to return status, text, and content type.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py -v
```

Expected: FAIL on missing routes or incorrect proxy behavior.

- [ ] **Step 3: Implement proxy routes**

In `selling_console.py`, delegate to debugger helpers:

```python
if parsed.path == "/api/pipeline/state":
    status, body = a2a_debugger._pipeline_state_response(a2a_debugger._query_params(self.path))
    _send_json(self, status, body)
    return
if parsed.path == "/api/task/get":
    status, body = a2a_debugger._task_get_response(a2a_debugger._query_params(self.path))
    _send_json(self, status, body)
    return
```

For POST:

```python
if parsed.path == "/api/message/stream":
    body = _read_json_body(self)
    server_url, payload = a2a_debugger._message_stream_body(body)
    with a2a_debugger._open_sse_stream(server_url, payload) as response:
        self.send_response(response.status)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for line in response:
            self.wfile.write(line)
            self.wfile.flush()
    return
if parsed.path == "/api/task/cancel":
    body = _read_json_body(self)
    status, response_body = a2a_debugger._task_cancel_response(body)
    _send_json(self, status, response_body)
    return
```

Wrap `ValueError` as `400` JSON and `HTTPError`, `URLError`, `TimeoutError`, `OSError` as proxy errors using debugger semantics.

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add scripts/a2a/selling_console.py tests/a2a/test_selling_console_script.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: proxy A2A routes for selling console"
```

## Task 3: Frontend Reducer Tests

**Files:**
- Create: `tests/a2a/test_selling_console_frontend.py`
- Modify: `scripts/a2a/selling_console_web/app.js`

- [ ] **Step 1: Write failing reducer tests**

Create `tests/a2a/test_selling_console_frontend.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "scripts" / "a2a" / "selling_console_web" / "app.js"


def run_node_script(source: str) -> dict:
    try:
        result = subprocess.run(["node", "-e", source], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        pytest.skip("node is not installed")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def reducer_harness(expression: str) -> dict:
    app_source = APP_JS.read_text(encoding="utf-8")
    script = f"""
const assert = require("assert");
global.window = {{}};
global.document = {{
  readyState: "loading",
  addEventListener() {{}},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
  getElementById() {{ return null; }}
}};
{app_source}
const reducers = window.SellingConsoleReducers;
const output = (() => {{
  {expression}
}})();
console.log(JSON.stringify(output));
"""
    return run_node_script(script)


def test_reducer_maps_pipeline_steps_to_console_sections() -> None:
    output = reducer_harness(
        """
        const state = reducers.createInitialState({serverUrl: "http://127.0.0.1:41299", cwd: "/workspace"});
        reducers.reducePipelinePayload(state, {
          metadata: {iac_code: {pipeline: {
            eventType: "step_completed",
            status: "working",
            taskId: "task-1",
            contextId: "ctx-1",
            sequence: 3,
            step: {id: "architecture_planning", name: "架构规划", status: "completed"}
          }}}
        });
        return {
          taskId: state.pipelineTaskId,
          contextId: state.contextId,
          sequence: state.lastSequence,
          architectureStatus: state.steps.architecture_planning.status
        };
        """
    )

    assert output == {
        "taskId": "task-1",
        "contextId": "ctx-1",
        "sequence": 3,
        "architectureStatus": "completed",
    }


def test_reducer_collects_candidate_details_from_tool_display() -> None:
    output = reducer_harness(
        """
        const state = reducers.createInitialState({});
        reducers.reducePipelinePayload(state, {
          snapshot: {
            status: "waiting_input",
            display: {
              candidateDetails: [{
                candidateName: "ECS 经典网络方案",
                candidateIndex: 0,
                summary: "VPC + ECS + EIP",
                totalMonthlyCost: "¥33.89/月",
                costItems: [{name: "ECS", spec: "1vCPU/1GiB", monthly_cost: "¥33.89/月"}]
              }]
            },
            pendingInput: {
              kind: "ask_user_question",
              prompt: "请选择方案",
              options: [{id: "0", label: "ECS 经典网络方案"}]
            }
          }
        });
        return {
          candidateCount: state.candidates.length,
          firstName: state.candidates[0].name,
          firstCost: state.candidates[0].totalMonthlyCost,
          pendingPrompt: state.pendingInput.prompt
        };
        """
    )

    assert output == {
        "candidateCount": 1,
        "firstName": "ECS 经典网络方案",
        "firstCost": "¥33.89/月",
        "pendingPrompt": "请选择方案",
    }


def test_reducer_clears_active_task_on_normal_handoff() -> None:
    output = reducer_harness(
        """
        const state = reducers.createInitialState({});
        state.pipelineTaskId = "pipeline-task";
        state.activeTaskId = "pipeline-task";
        reducers.reducePipelinePayload(state, {
          metadata: {iac_code: {pipeline: {
            eventType: "pipeline_handoff_ready",
            taskId: "pipeline-task",
            contextId: "ctx-1",
            status: "completed",
            data: {action: "switch_to_normal", targetMode: "normal"}
          }}}
        });
        return {
          normalHandoffReady: state.normalHandoffReady,
          activeTaskId: state.activeTaskId,
          contextId: state.contextId
        };
        """
    )

    assert output == {
        "normalHandoffReady": True,
        "activeTaskId": "",
        "contextId": "ctx-1",
    }
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_frontend.py -v
```

Expected: FAIL because `createInitialState` and `reducePipelinePayload` are not implemented.

- [ ] **Step 3: Implement reducer helpers**

In `app.js`, expose pure helpers:

```javascript
(function () {
  const STEP_ORDER = [
    "intent_parsing",
    "architecture_planning",
    "evaluate_candidates",
    "confirm_and_select",
    "deploying"
  ];

  const STEP_LABELS = {
    intent_parsing: "需求理解",
    architecture_planning: "架构规划",
    evaluate_candidates: "方案评估",
    confirm_and_select: "方案选择",
    deploying: "确认部署"
  };

  function createInitialState(defaults = {}) {
    const steps = {};
    STEP_ORDER.forEach((id) => {
      steps[id] = {id, label: STEP_LABELS[id], status: "pending", events: []};
    });
    return {
      defaults,
      serverUrl: defaults.serverUrl || "",
      cwd: defaults.cwd || "",
      contextId: "",
      pipelineTaskId: "",
      activeTaskId: "",
      lastSequence: 0,
      status: "idle",
      normalHandoffReady: false,
      steps,
      candidates: [],
      selectedCandidateIndex: null,
      pendingInput: null,
      permission: null,
      diagnostics: {requests: [], sse: [], snapshots: []}
    };
  }
```

Implement:

- `extractPipelineEnvelope(payload)` tolerant of metadata and snapshot wrappers.
- `normalizeStepId(step)` mapping candidate sub-step events to `evaluate_candidates`.
- `upsertCandidate(state, candidate)`.
- `reducePipelinePayload(state, payload)`.
- `candidateFromDisplayItem(item)`.
- `pendingInputFromSnapshot(snapshot)`.

- [ ] **Step 4: Run reducer tests and verify GREEN**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_frontend.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add scripts/a2a/selling_console_web/app.js tests/a2a/test_selling_console_frontend.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: add selling console frontend reducer"
```

## Task 4: Screenshot-Matching Static UI

**Files:**
- Modify: `scripts/a2a/selling_console_web/index.html`
- Modify: `scripts/a2a/selling_console_web/styles.css`
- Modify: `scripts/a2a/selling_console_web/app.js`
- Modify: `tests/a2a/test_selling_console_script.py`

- [ ] **Step 1: Add failing static UI contract test**

Append:

```python
def test_index_html_contains_screenshot_layout_regions() -> None:
    console = load_module()

    html = console.render_index_html(
        console.SellingConsoleConfig(
            host="127.0.0.1",
            port=41980,
            default_server_url="http://127.0.0.1:41299",
            default_cwd="/workspace/demo",
        )
    )

    for expected in [
        'class="topbar"',
        'id="workflow-panel"',
        'id="plans-grid"',
        'id="composer-input"',
        'id="send-button"',
        'id="deep-think-button"',
        'id="debug-drawer"',
        "需求理解",
        "架构规划",
        "方案评估",
        "方案选择",
        "您的购买方案",
        "内容由 AI 生成，方案与价格仅供参考",
    ]:
        assert expected in html
```

Add CSS contract:

```python
def test_styles_define_console_layout_tokens() -> None:
    css = (SCRIPT_PATH.parent / "selling_console_web" / "styles.css").read_text(encoding="utf-8")

    for expected in [
        "--aliyun-orange",
        ".console-shell",
        ".workflow-panel",
        ".plan-card",
        ".price",
        ".utility-rail",
        "@media (max-width: 980px)",
    ]:
        assert expected in css
```

Add JS syntax check:

```python
def test_frontend_javascript_is_syntax_valid() -> None:
    app_js = SCRIPT_PATH.parent / "selling_console_web" / "app.js"
    try:
        result = subprocess.run(["node", "--check", str(app_js)], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        pytest.skip("node is not installed")

    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run static UI tests and verify RED**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py::test_index_html_contains_screenshot_layout_regions tests/a2a/test_selling_console_script.py::test_styles_define_console_layout_tokens tests/a2a/test_selling_console_script.py::test_frontend_javascript_is_syntax_valid -v
```

Expected: FAIL on missing layout regions and CSS tokens.

- [ ] **Step 3: Implement HTML shell**

Replace the stub HTML with:

```html
<body>
  <header class="topbar" aria-label="阿里云控制台导航">
    <button class="icon-button menu-button" type="button" aria-label="打开菜单"><span></span><span></span><span></span></button>
    <div class="brand-mark"><span class="brand-symbol">▱</span><span>阿里云</span></div>
    <button class="nav-pill" type="button">⌂ 工作台</button>
    <button class="nav-pill" type="button">▤ 账号全部资源⌄</button>
    <button class="nav-pill" type="button">⌖ 华东1（杭州）⌄</button>
    <label class="search-box"><span>⌕</span><input type="search" placeholder="搜索..."></label>
    <nav class="top-links" aria-label="控制台链接"><a>文档</a><a>费用</a><a>备案</a><a>工单</a></nav>
    <div class="user-menu"><span>gaojiajia@test...</span><small>RAM 用户</small><span class="avatar"></span></div>
  </header>
  <main class="console-shell">
    <aside class="assistant-rail" aria-label="AI 助手"><div class="bot-avatar">🤖</div></aside>
    <section class="workflow-panel" id="workflow-panel">
      <div id="status-alert" class="status-alert" hidden></div>
      <div id="step-list" class="step-list"></div>
      <section class="composer-card" aria-label="继续补充需求">
        <textarea id="composer-input" placeholder="继续补充您的需求，比如降低成本、切换地域或确认方案"></textarea>
        <div class="composer-actions">
          <button id="deep-think-button" type="button" class="think-button">✧ 深度思考</button>
          <span class="composer-spacer"></span>
          <button id="attachment-button" type="button" class="icon-button" aria-label="添加附件">⌕</button>
          <button id="send-button" type="button" class="send-button" aria-label="发送">➤</button>
        </div>
      </section>
      <p class="ai-disclaimer">内容由 AI 生成，方案与价格仅供参考，请以实际部署结果为准。</p>
    </section>
    <section class="plan-area" aria-label="购买方案">
      <div class="plan-area-header">
        <h1>您的购买方案</h1>
        <div class="connection-controls">
          <input id="server-url" value="__DEFAULT_SERVER_URL__" aria-label="A2A server URL">
          <input id="cwd" value="__DEFAULT_CWD__" aria-label="A2A cwd">
          <button id="health-button" type="button">连接检查</button>
          <button id="fetch-state-button" type="button">同步状态</button>
          <button id="cancel-button" type="button">取消任务</button>
        </div>
      </div>
      <div id="plans-grid" class="plans-grid"></div>
      <details id="debug-drawer" class="debug-drawer">
        <summary>调试信息</summary>
        <div id="debug-output" class="debug-output"></div>
      </details>
    </section>
    <aside class="utility-rail" aria-label="控制台工具"><button>▣</button><button>APP</button><button>⌘</button><button>⚙</button><button>✦</button></aside>
  </main>
</body>
```

- [ ] **Step 4: Implement screenshot CSS**

Set desktop dimensions close to the screenshot:

```css
:root {
  --aliyun-orange: #ff6a00;
  --accent-blue: #4f7cff;
  --success-green: #46c22f;
  --text-strong: #1f2329;
  --text-muted: #858b99;
  --line: #d9e2ef;
  --panel: #ffffff;
  --soft-bg: #f7faff;
}

body {
  margin: 0;
  color: var(--text-strong);
  background: #fff;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.topbar {
  display: grid;
  grid-template-columns: 56px 132px auto auto auto minmax(280px, 1fr) auto auto;
  align-items: center;
  gap: 10px;
  height: 64px;
  padding: 0 18px;
  border-bottom: 1px solid #edf1f7;
  box-shadow: 0 2px 14px rgba(31, 35, 41, 0.08);
}

.console-shell {
  display: grid;
  grid-template-columns: 48px minmax(460px, 640px) minmax(520px, 1fr) 64px;
  min-height: calc(100vh - 64px);
}

.workflow-panel {
  position: relative;
  padding: 42px 22px 20px 16px;
  border-right: 1px solid #e7edf5;
  background:
    radial-gradient(circle at 10% 100%, rgba(237, 244, 255, 0.95), transparent 38%),
    radial-gradient(circle at 90% 100%, rgba(255, 238, 246, 0.75), transparent 34%),
    #fff;
}

.plan-card {
  min-height: 260px;
  border: 1px solid #dde5f0;
  border-radius: 8px;
  padding: 28px;
  background: #fff;
}

.price {
  color: var(--aliyun-orange);
  font-size: 34px;
  font-weight: 800;
}

@media (max-width: 980px) {
  .topbar { grid-template-columns: 48px 120px 1fr; }
  .topbar .nav-pill,
  .topbar .top-links,
  .topbar .user-menu { display: none; }
  .console-shell { grid-template-columns: 1fr; }
  .assistant-rail,
  .utility-rail { display: none; }
  .workflow-panel { border-right: 0; }
  .plans-grid { grid-template-columns: 1fr; }
}
```

- [ ] **Step 5: Run static UI tests and verify GREEN**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py::test_index_html_contains_screenshot_layout_regions tests/a2a/test_selling_console_script.py::test_styles_define_console_layout_tokens tests/a2a/test_selling_console_script.py::test_frontend_javascript_is_syntax_valid -v
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add scripts/a2a/selling_console_web/index.html scripts/a2a/selling_console_web/styles.css scripts/a2a/selling_console_web/app.js tests/a2a/test_selling_console_script.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: add selling console visual shell"
```

## Task 5: DOM Controller and Complete Interaction

**Files:**
- Modify: `scripts/a2a/selling_console_web/app.js`
- Modify: `tests/a2a/test_selling_console_frontend.py`

- [ ] **Step 1: Add failing interaction reducer tests**

Append frontend tests for composer payload and candidate selection:

```python
def test_build_stream_payload_uses_active_task_before_handoff() -> None:
    output = reducer_harness(
        """
        const state = reducers.createInitialState({serverUrl: "http://server", cwd: "/workspace"});
        state.contextId = "ctx-1";
        state.pipelineTaskId = "pipeline-task";
        state.activeTaskId = "active-task";
        return reducers.buildStreamPayload(state, "继续部署");
        """
    )

    assert output == {
        "serverUrl": "http://server",
        "cwd": "/workspace",
        "contextId": "ctx-1",
        "taskId": "active-task",
        "prompt": "继续部署",
    }


def test_candidate_selection_prompt_uses_zero_based_index() -> None:
    output = reducer_harness(
        """
        const state = reducers.createInitialState({});
        state.candidates = [
          {name: "ECS 经典网络方案", candidateIndex: 0},
          {name: "轻量应用服务器一体化方案", candidateIndex: 1}
        ];
        reducers.selectCandidate(state, 1);
        return {
          selected: state.selectedCandidateIndex,
          prompt: reducers.promptForSelectedCandidate(state)
        };
        """
    )

    assert output == {"selected": 1, "prompt": "选择方案1"}
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_frontend.py -v
```

Expected: FAIL on missing `buildStreamPayload`, `selectCandidate`, or `promptForSelectedCandidate`.

- [ ] **Step 3: Implement DOM controller and remaining pure helpers**

Add:

```javascript
function buildStreamPayload(state, prompt) {
  const taskId = state.normalHandoffReady ? "" : state.activeTaskId || state.pipelineTaskId || "";
  return {
    serverUrl: state.serverUrl,
    cwd: state.cwd,
    contextId: state.contextId,
    taskId,
    prompt
  };
}

function selectCandidate(state, candidateIndex) {
  state.selectedCandidateIndex = Number(candidateIndex);
}

function promptForSelectedCandidate(state) {
  if (state.selectedCandidateIndex === null || state.selectedCandidateIndex === undefined) {
    return "";
  }
  return `选择方案${state.selectedCandidateIndex}`;
}
```

Add DOM functions:

- `init()` reads `window.SELLING_CONSOLE_DEFAULTS`, creates state, binds buttons, renders empty state.
- `renderSteps()` creates workflow cards and candidate radio cards.
- `renderPlans()` creates right-side plan cards.
- `sendComposerMessage()` validates input, builds payload, streams SSE, and stops on `input_required`.
- `healthCheck()`, `fetchState()`, `cancelTask()`.
- `appendDiagnostic(kind, value)`, `renderDebug()`.
- `window.SellingConsoleDebug.loadDemoCandidates()` for browser verification without a live A2A server. It should inject two candidates matching the screenshot copy and re-render the workflow and plan cards.

Use only `textContent`, `createElement`, and `setAttribute` for dynamic content. Avoid assigning user-controlled values to `innerHTML`.

- [ ] **Step 4: Run frontend tests and syntax check**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_frontend.py tests/a2a/test_selling_console_script.py::test_frontend_javascript_is_syntax_valid -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add scripts/a2a/selling_console_web/app.js tests/a2a/test_selling_console_frontend.py
PATH="$HOME/.local/bin:$PATH" git commit -m "feat: wire selling console interactions"
```

## Task 6: Documentation and Full Verification

**Files:**
- Modify: `scripts/README.md`

- [ ] **Step 1: Add failing README test or update existing script docs assertion**

Add a small assertion to `tests/a2a/test_selling_console_script.py`:

```python
def test_scripts_readme_mentions_selling_console() -> None:
    readme = (Path(__file__).resolve().parents[2] / "scripts" / "README.md").read_text(encoding="utf-8")

    assert "a2a/selling_console.py" in readme
    assert "Selling pipeline console" in readme
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py::test_scripts_readme_mentions_selling_console -v
```

Expected: FAIL until README is updated.

- [ ] **Step 3: Update scripts README**

Add a table row and command:

```markdown
| `a2a/selling_console.py` | Selling pipeline console for local A2A interactions. |
```

Add usage:

```bash
PATH="$HOME/.local/bin:$PATH" \
uv run python scripts/a2a/selling_console.py --port 41980 \
  --default-server-url http://127.0.0.1:41299 \
  --default-cwd "$PWD"
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_selling_console_script.py tests/a2a/test_selling_console_frontend.py -v
```

Expected: PASS.

- [ ] **Step 5: Run relevant existing debugger tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_pipeline_debugger_script.py tests/a2a/test_selling_console_script.py tests/a2a/test_selling_console_frontend.py -v
```

Expected: PASS.

- [ ] **Step 6: Start local console for browser verification**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run python scripts/a2a/selling_console.py --port 41980 --default-cwd "$PWD"
```

Expected output includes:

```text
A2A selling pipeline console: http://127.0.0.1:41980
```

- [ ] **Step 7: Browser verification**

Open `http://127.0.0.1:41980` in the in-app browser.

Verify:

- Desktop viewport shows topbar, left workflow panel, right plan title, and utility rail.
- Left panel resembles the screenshot: four main workflow cards, expanded plan selection card, bottom composer, disclaimer.
- Right side renders empty plan state without overlap.
- With no A2A server running, use `window.SellingConsoleDebug.loadDemoCandidates()` from the browser console; verify two candidate cards show in both left and right areas.
- Narrow viewport around 390px stacks the plan area below the workflow panel and no text overlaps.

- [ ] **Step 8: Commit Task 6**

Run:

```bash
git add scripts/README.md tests/a2a/test_selling_console_script.py
PATH="$HOME/.local/bin:$PATH" git commit -m "docs: describe selling pipeline console"
```

## Final Verification

- [ ] Run focused suite:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/a2a/test_pipeline_debugger_script.py tests/a2a/test_selling_console_script.py tests/a2a/test_selling_console_frontend.py -v
```

- [ ] Run lint:

```bash
PATH="$HOME/.local/bin:$PATH" make lint
```

- [ ] If i18n baseline is still missing `src/iac_code/i18n/messages.pot`, report that full `make test` remains blocked by the pre-existing missing POT file. Do not treat that as caused by this feature.

## Self-Review

- Spec coverage:
  - New local script and static assets are covered by Tasks 1, 4, and 5.
  - A2A proxy reuse is covered by Tasks 1 and 2.
  - Full selling interaction is covered by Tasks 3 and 5.
  - Screenshot visual match is covered by Task 4 and browser verification.
  - Tests and docs are covered by Tasks 1 through 6.
- Placeholder scan:
  - No placeholder markers, incomplete sections, or missing file paths are present.
- Type consistency:
  - Python config is consistently named `SellingConsoleConfig`.
  - Frontend state fields are consistently named `pipelineTaskId`, `activeTaskId`, `normalHandoffReady`, `pendingInput`, and `selectedCandidateIndex`.
