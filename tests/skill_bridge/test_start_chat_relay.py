from __future__ import annotations

import asyncio
import importlib.util
import json
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from iac_code.a2a.app import create_app
from iac_code.a2a.executor import publish_stream_event as publish_stream_event_default
from iac_code.a2a.pipeline_events import PipelineA2AContext, PipelineEventTranslator
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore
from iac_code.a2a.pipeline_stream import PipelineA2AEventPublisher
from iac_code.a2a.transports.dispatcher import create_runtime_components
from iac_code.types.stream_events import PermissionRequestEvent, SubPipelineStreamEvent, TextDeltaEvent

ROOT = Path(__file__).resolve().parents[2]
RELAY_PATH = Path(__file__).with_name("start_chat_relay.py")
BRIDGE_PATH = ROOT / "skills/alicloud-ros-agent/scripts/ros_agent.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relay = _load_module("start_chat_test_relay", RELAY_PATH)
bridge = _load_module("start_chat_test_bridge", BRIDGE_PATH)


def _clear_code_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in bridge.ACCESS_KEY_ID_ENV_NAMES + bridge.ACCESS_KEY_SECRET_ENV_NAMES + bridge.SECURITY_TOKEN_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_relay_accepts_only_published_start_chat_parameters() -> None:
    parameters = relay.parse_start_chat_request(
        "/?Action=StartChat&Version=2019-09-10&Query=hello&AgentVersion=V2&"
        "EnablePartialMessage=true&EnableThinking=false&Mode=IaCCodeNormal&RegionId=cn-hangzhou&"
        "Attachments.1.Type=image&Attachments.1.MimeType=image%2Fpng&Attachments.1.OssObjectKey=demo.png",
        b"",
        {"x-acs-action": "StartChat"},
    )

    assert parameters == {
        "Query": "hello",
        "AgentVersion": "V2",
        "EnablePartialMessage": "true",
        "EnableThinking": "false",
        "Mode": "IaCCodeNormal",
        "RegionId": "cn-hangzhou",
        "Attachments.1.Type": "image",
        "Attachments.1.MimeType": "image/png",
        "Attachments.1.OssObjectKey": "demo.png",
    }
    with pytest.raises(relay.StartChatRequestError, match="PipelineName"):
        relay.parse_start_chat_request(
            "/?Action=StartChat&Query=hello&PipelineName=selling",
            b"",
            {"x-acs-action": "StartChat"},
        )
    with pytest.raises(relay.StartChatRequestError, match="RPC root"):
        relay.parse_start_chat_request(
            "/health?Action=StartChat&Query=hello",
            b"",
            {"x-acs-action": "StartChat"},
        )


def test_relay_accepts_only_published_stop_chat_parameters() -> None:
    parameters = relay.parse_stop_chat_request(
        "/?Action=StopChat&Version=2019-09-10&SessionId=session-1&AgentVersion=V2",
        b"",
        {"x-acs-action": "StopChat"},
    )

    assert parameters == {"SessionId": "session-1", "AgentVersion": "V2"}
    with pytest.raises(relay.StartChatRequestError, match="Query"):
        relay.parse_stop_chat_request(
            "/?Action=StopChat&SessionId=session-1&Query=cancel",
            b"",
            {"x-acs-action": "StopChat"},
        )
    with pytest.raises(relay.StartChatRequestError, match="SessionId"):
        relay.parse_stop_chat_request(
            "/?Action=StopChat",
            b"",
            {"x-acs-action": "StopChat"},
        )


def test_relay_stop_chat_calls_a2a_cancel_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:1/",
        pipeline_a2a_url="http://127.0.0.1:2/",
        workspace=str(tmp_path),
        ssl_context=_tls_context(tmp_path),
    )
    session = relay._Session(session_id="session-1", mode="IaCCodePipeline", task_id="task-1")
    server.sessions[session.session_id] = session
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "request-1",
                    "result": {"id": "task-1", "status": {"state": "TASK_STATE_CANCELED"}},
                }
            ).encode("utf-8")

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

    monkeypatch.setattr(relay, "build_opener", lambda *_args: Opener())
    try:
        status = server.stop_session("session-1")

        assert status == "Stopped"
        assert captured["url"] == "http://127.0.0.1:2/"
        assert captured["payload"]["method"] == "CancelTask"
        assert captured["payload"]["params"] == {"id": "task-1"}
    finally:
        server.server_close()


def test_pipeline_start_chat_requests_rich_a2a_candidate_projection(tmp_path: Path) -> None:
    server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:1/",
        pipeline_a2a_url="http://127.0.0.1:2/",
        workspace=str(tmp_path),
        ssl_context=_tls_context(tmp_path),
    )
    captured = []

    def consume(_session, call, payload, upstream_url):
        captured.append((payload, upstream_url))
        call.events.put(relay._END)

    server._consume_a2a = consume
    try:
        session = relay._Session(session_id="session-1", mode="IaCCodePipeline")
        call = server.start_a2a_call(
            session,
            {
                "Query": "deploy",
                "Mode": "IaCCodePipeline",
                "EnableThinking": "true",
                "RegionId": "cn-hangzhou",
            },
        )
        call.thread.join(timeout=2)
        payload, upstream_url = captured[0]
        iac_code = payload["params"]["message"]["metadata"]["iac_code"]
        assert iac_code["candidatePresentation"] == "rich-v1"
        assert upstream_url == "http://127.0.0.1:2/"

        handoff_event = {
            "result": {
                "statusUpdate": {
                    "taskId": "task-1",
                    "contextId": "session-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "metadata": {
                        "iac_code": {
                            "pipeline": {
                                "eventType": "pipeline_handoff_ready",
                                "visibility": "committed",
                                "data": {"action": "switch_to_normal", "targetMode": "normal"},
                            }
                        }
                    },
                }
            }
        }
        server._observe_sideband_state(session, handoff_event)
        assert session.normal_handoff_ready is True
        continued = server.start_a2a_call(
            session,
            {"Query": "delete the deployed stack", "Mode": "IaCCodePipeline", "EnableThinking": "true"},
        )
        continued.thread.join(timeout=2)
        continued_message = captured[1][0]["params"]["message"]
        assert continued_message["contextId"] == "session-1"
        assert "taskId" not in continued_message
        assert captured[1][1] == "http://127.0.0.1:2/"
    finally:
        server.server_close()


def test_relay_projects_http_200_json_rpc_error_as_failed_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            yield json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "request-1",
                    "error": {
                        "code": -32602,
                        "message": "permission_resume_invalid: canonical permission request changed.",
                    },
                }
            ).encode("utf-8")

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 3
            return Response()

    monkeypatch.setattr(relay, "build_opener", lambda *_args: Opener())
    server = object.__new__(relay.StartChatRelay)
    server.upstream_timeout = 3
    session = relay._Session(session_id="session-1", mode="IaCCodeNormal")
    call = relay._UpstreamCall()

    server._consume_a2a(session, call, {"jsonrpc": "2.0"}, "http://127.0.0.1:1/")

    assert call.events.get_nowait() == {
        "id": "session-1",
        "object": "response",
        "status": "failed",
        "error": {
            "code": "-32602",
            "message": "permission_resume_invalid: canonical permission request changed.",
        },
    }
    assert call.events.get_nowait() is relay._END


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tls_context(tmp_path: Path) -> ssl.SSLContext:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for the local HTTPS StartChat relay")
    key = tmp_path / "relay-key.pem"
    certificate = tmp_path / "relay-cert.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context


def _start_uvicorn(app, port: int):
    uvicorn = pytest.importorskip("uvicorn")
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="test-iac-code-a2a", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, thread


def _aliyun_start_chat_command(
    aliyun: str,
    endpoint: str,
    query: str,
    *,
    session_id: str | None = None,
    mode: str = "normal",
) -> list[str]:
    command = bridge.build_command(
        SimpleNamespace(
            aliyun_path=aliyun,
            endpoint="ros.aliyuncs.com",
            connect_timeout=3,
            read_timeout=15,
            profile=None,
            region_id="cn-hangzhou",
            no_thinking=True,
            mode=mode,
            session_id=session_id,
        ),
        query,
        None,
        [],
    )
    command[command.index("--endpoint") + 1] = endpoint
    query_index = command.index("--Query")
    command[query_index:query_index] = [
        "--skip-secure-verify",
        "--mode",
        "AK",
        "--access-key-id",
        "fake-access-key-id",
        "--access-key-secret",
        "fake-access-key-secret",
        "--retry-count",
        "0",
    ]
    return command


def _aliyun_start_chat(
    aliyun: str,
    endpoint: str,
    query: str,
    *,
    session_id: str | None = None,
    mode: str = "normal",
) -> tuple[str, str]:
    command = _aliyun_start_chat_command(
        aliyun,
        endpoint,
        query,
        session_id=session_id,
        mode=mode,
    )
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, bridge.sanitize_text(completed.stderr, 1000)
    return completed.stdout, completed.stderr


def _aliyun_stop_chat(aliyun: str, endpoint: str, session_id: str) -> dict:
    command = bridge.build_stop_command(
        {
            "aliyunPath": aliyun,
            "endpoint": endpoint,
            "connectTimeout": 3,
            "profile": None,
            "regionId": "cn-hangzhou",
        },
        session_id,
    )
    input_index = command.index("--AgentVersion")
    command[input_index:input_index] = [
        "--mode",
        "AK",
        "--access-key-id",
        "fake-access-key-id",
        "--access-key-secret",
        "fake-access-key-secret",
        "--retry-count",
        "0",
    ]
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, bridge.sanitize_text(completed.stderr, 1000)
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _summarize_sse(
    stdout: str,
    stderr: str,
    *,
    session_id: str | None = None,
    mode: str = "normal",
) -> dict:
    summary = bridge.StreamSummary(session_id, mode=mode)
    diagnostics = []
    for payload, raw in bridge.iter_sse_payloads(stdout.splitlines(keepends=True)):
        if payload is None:
            summary.malformed_event_count += 1
            diagnostics.append(raw)
        else:
            summary.apply(payload)
    return summary.to_result(0, stderr or "\n".join(diagnostics))


def _permission_response_query(permission: dict, decision: str) -> str:
    return "{} {}".format(
        bridge.PERMISSION_QUERY_PREFIX,
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": permission["requestTaskId"],
                "contextId": permission["contextId"],
                "inputId": permission["inputId"],
                "toolUseId": permission["toolUseId"],
                "decision": decision,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def test_code_transport_streams_through_sdk_to_endpoint_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_code_credential_env(monkeypatch)
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:1/",
        workspace=str(tmp_path),
        ssl_context=_tls_context(tmp_path),
    )
    captured = {}
    working_event = {
        "result": {
            "statusUpdate": {
                "taskId": "task-code-1",
                "contextId": "session-code-1",
                "status": {
                    "state": "TASK_STATE_WORKING",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "working"}]},
                },
                "metadata": {"iac_code": {}, "iacCodeSessionId": "iac-code-1"},
            }
        }
    }
    completed_event = {
        "result": {
            "statusUpdate": {
                "taskId": "task-code-1",
                "contextId": "session-code-1",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "code transport done"}]},
                },
                "metadata": {
                    "iac_code": {"assistantFinal": {"complete": True}},
                    "iacCodeSessionId": "iac-code-1",
                },
            }
        }
    }

    def start_a2a_call(session, parameters):
        captured["parameters"] = parameters
        call = relay._UpstreamCall()
        captured["call"] = call
        call.events.put(working_event)
        return call

    relay_server.start_a2a_call = start_a2a_call
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-code-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])

    class FakeCredentials:
        def get_access_key_id(self):
            return "fake-access-key-id"

        def get_access_key_secret(self):
            return "fake-access-key-secret"

        def get_security_token(self):
            return "fake-security-token"

    class FakeProvider:
        def __init__(self, profile_name=None):
            captured["profile"] = profile_name

        def get_credentials(self):
            return FakeCredentials()

    sdk = bridge._load_code_sdk()
    sdk["CLIProfileCredentialsProvider"] = FakeProvider
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: sdk)
    monkeypatch.setattr(bridge, "_selected_cli_profile", lambda profile: (profile, "AK"))
    args = SimpleNamespace(
        aliyun_path="not-used",
        transport="code",
        endpoint=endpoint,
        connect_timeout=3,
        read_timeout=15,
        profile="sdk-profile",
        region_id="cn-hangzhou",
        no_thinking=True,
        mode="normal",
        session_id=None,
    )
    first_payload = threading.Event()
    outcome = {}

    def consume() -> None:
        try:
            outcome["result"] = bridge._consume_start_chat(
                args,
                tmp_path,
                "create a VPC",
                None,
                [],
                on_payload=lambda _payload, _summary: first_payload.set(),
            )
        except BaseException as exc:  # pragma: no cover - asserted in the main test thread
            outcome["error"] = exc

    consumer_thread = threading.Thread(target=consume, name="test-code-consumer", daemon=True)

    try:
        consumer_thread.start()

        assert first_payload.wait(timeout=3), "a flushed SSE event must be delivered before the stream closes"
        assert consumer_thread.is_alive()

        captured["call"].events.put(completed_event)
        captured["call"].events.put(relay._END)
        consumer_thread.join(timeout=5)

        assert not consumer_thread.is_alive()
        assert "error" not in outcome
        result = outcome["result"]

        assert result["state"] == "turn-completed"
        assert result["finalText"] == "code transport done"
        assert captured["profile"] == "sdk-profile"
        assert captured["parameters"]["Query"] == "create a VPC"
        assert captured["parameters"]["Mode"] == "IaCCodeNormal"
    finally:
        if consumer_thread.is_alive() and "call" in captured:
            captured["call"].events.put(completed_event)
            captured["call"].events.put(relay._END)
            consumer_thread.join(timeout=5)
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)


def test_stop_chat_round_trip_through_real_aliyun_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    aliyun = shutil.which("aliyun")
    if aliyun is None:
        pytest.skip("Alibaba Cloud CLI is not installed")
    metrics_path = tmp_path / "relay-metrics.json"
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:1/",
        pipeline_a2a_url="http://127.0.0.1:2/",
        workspace=str(tmp_path),
        ssl_context=_tls_context(tmp_path),
        metrics_path=str(metrics_path),
    )
    session = relay._Session(session_id="session-cli-stop", mode="IaCCodePipeline", task_id="task-1")
    relay_server.sessions[session.session_id] = session

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return json.dumps(
                {"jsonrpc": "2.0", "result": {"id": "task-1", "status": {"state": "TASK_STATE_CANCELED"}}}
            ).encode("utf-8")

    class Opener:
        def open(self, _request, timeout):
            assert timeout == relay_server.upstream_timeout
            return Response()

    monkeypatch.setattr(relay, "build_opener", lambda *_args: Opener())
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-stop-chat-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])
    try:
        result = _aliyun_stop_chat(aliyun, endpoint, session.session_id)

        assert result["Status"] == "Stopped"
        assert result["SessionId"] == session.session_id
        assert isinstance(result["RequestId"], str)
        deadline = time.monotonic() + 2
        while not metrics_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        assert metrics["requests"] == [
            {
                "action": "StopChat",
                "durationMs": metrics["requests"][0]["durationMs"],
                "finishedAtUnixMs": metrics["requests"][0]["finishedAtUnixMs"],
                "sessionId": session.session_id,
                "startedAtUnixMs": metrics["requests"][0]["startedAtUnixMs"],
                "stopStatus": "Stopped",
            }
        ]
    finally:
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)


def test_stop_chat_cancels_live_a2a_stream_through_real_aliyun_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    aliyun = shutil.which("aliyun")
    if aliyun is None:
        pytest.skip("Alibaba Cloud CLI is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)
    started: queue.Queue[bool] = queue.Queue()

    class SlowLoop:
        async def run_streaming(self, _prompt: str):
            started.put(True)
            yield TextDeltaEvent(text="working before cancellation")
            await asyncio.sleep(60)

    def runtime_factory(options):
        return SimpleNamespace(agent_loop=SlowLoop(), session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime_factory)
    a2a_port = _free_port()
    app = create_app(
        host="127.0.0.1",
        port=a2a_port,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a-state",
        artifact_dir=tmp_path / "artifacts",
    )
    a2a_server, a2a_thread = _start_uvicorn(app, a2a_port)
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:{}/".format(a2a_port),
        workspace=str(workspace),
        ssl_context=_tls_context(tmp_path),
    )
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-live-stop-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])
    command = bridge.build_command(
        SimpleNamespace(
            aliyun_path=aliyun,
            endpoint=endpoint,
            connect_timeout=3,
            read_timeout=30,
            profile=None,
            region_id="cn-hangzhou",
            no_thinking=True,
            mode="normal",
            session_id=None,
        ),
        "run until canceled",
        None,
        [],
    )
    query_index = command.index("--Query")
    command[query_index:query_index] = [
        "--mode",
        "AK",
        "--access-key-id",
        "fake-access-key-id",
        "--access-key-secret",
        "fake-access-key-secret",
        "--retry-count",
        "0",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert started.get(timeout=10) is True
        deadline = time.monotonic() + 10
        session = None
        while time.monotonic() < deadline:
            with relay_server.sessions_lock:
                values = list(relay_server.sessions.values())
            if values and values[0].task_id:
                session = values[0]
                break
            time.sleep(0.05)
        assert session is not None

        stopped = _aliyun_stop_chat(aliyun, endpoint, session.session_id)
        stdout, stderr = process.communicate(timeout=15)
        result = _summarize_sse(stdout, stderr, session_id=session.session_id)

        assert stopped["Status"] == "Stopped"
        assert result["state"] == "canceled"
        assert result["sessionId"] == session.session_id
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)
        a2a_server.should_exit = True
        a2a_thread.join(timeout=10)


@pytest.mark.parametrize(("decision", "allowed"), [("allow_once", True), ("deny", False)])
def test_normal_permission_round_trip_through_real_aliyun_cli_and_a2a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: str,
    allowed: bool,
) -> None:
    aliyun = shutil.which("aliyun")
    if aliyun is None:
        pytest.skip("Alibaba Cloud CLI is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)
    decisions: queue.Queue[bool] = queue.Queue()
    prompts: queue.Queue[str] = queue.Queue()

    from iac_code.agent.message import Message, ToolUseBlock
    from iac_code.services.permission_wait import canonical_digest
    from iac_code.services.session_storage import SessionStorage

    class PermissionLoop:
        def __init__(self, options):
            self.options = options

        async def run_streaming(self, prompt: str):
            prompts.put(prompt)
            tool_use = ToolUseBlock(id="tool-normal-1", name="bash", input={"cmd": "pwd"})
            assistant = Message(role="assistant", content=[tool_use])
            storage = SessionStorage()
            storage.ensure_v2_session_dir_for_new_session(str(self.options.cwd), str(self.options.session_id))
            storage.append(str(self.options.cwd), str(self.options.session_id), assistant)
            response = asyncio.get_running_loop().create_future()
            yield PermissionRequestEvent(
                tool_name="bash",
                tool_input={"cmd": "pwd"},
                tool_use_id="tool-normal-1",
                response_future=response,
                continuation_frame={
                    "assistantMessageRef": "session.jsonl:0",
                    "assistantMessageDigest": canonical_digest(
                        [block.model_dump(mode="json") for block in assistant.content]
                    ),
                    "orderedToolUseIds": ["tool-normal-1"],
                    "currentIndex": 0,
                    "decisions": [
                        {"toolUseId": "tool-normal-1", "state": "pending", "source": None, "deniedResult": None}
                    ],
                },
                audit_context={"session_id": str(self.options.session_id), "cwd": str(self.options.cwd)},
            )
            decisions.put(response.result())
            yield TextDeltaEvent(text="normal permission resolved")

    def runtime_factory(options):
        return SimpleNamespace(agent_loop=PermissionLoop(options), session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime_factory)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_k: True)

    a2a_port = _free_port()
    app = create_app(
        host="127.0.0.1",
        port=a2a_port,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a-state",
        artifact_dir=tmp_path / "artifacts",
    )
    a2a_server, a2a_thread = _start_uvicorn(app, a2a_port)
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:{}/".format(a2a_port),
        workspace=str(workspace),
        ssl_context=_tls_context(tmp_path),
    )
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-start-chat-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])

    try:
        first_stdout, first_stderr = _aliyun_start_chat(aliyun, endpoint, "request normal permission")
        first = _summarize_sse(first_stdout, first_stderr)
        assert first["state"] == "input-required"
        assert first["inputRequired"]["kind"] == "permission"
        assert first["inputRequired"]["permissionClass"] == "normal"
        assert first["sessionId"]

        permission = first["inputRequired"]
        response_query = _permission_response_query(permission, decision)
        second_stdout, second_stderr = _aliyun_start_chat(
            aliyun,
            endpoint,
            response_query,
            session_id=first["sessionId"],
        )
        second = _summarize_sse(second_stdout, second_stderr, session_id=first["sessionId"])

        assert decisions.get(timeout=2) is allowed
        assert prompts.get(timeout=2) == "request normal permission"
        assert second["ok"] is True
        assert second["state"] == "turn-completed"
        assert "normal permission resolved" in second["finalText"]
        assert second["sessionId"] == first["sessionId"]
    finally:
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)
        a2a_server.should_exit = True
        a2a_thread.join(timeout=10)


def test_normal_consecutive_permissions_return_at_each_serial_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    aliyun = shutil.which("aliyun")
    if aliyun is None:
        pytest.skip("Alibaba Cloud CLI is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)
    decisions: queue.Queue[tuple[str, bool]] = queue.Queue()

    from iac_code.agent.message import Message, ToolUseBlock
    from iac_code.services.permission_wait import canonical_digest
    from iac_code.services.session_storage import SessionStorage

    class ConsecutivePermissionLoop:
        def __init__(self, options):
            self.options = options

        async def run_streaming(self, _prompt: str):
            storage = SessionStorage()
            storage.ensure_v2_session_dir_for_new_session(str(self.options.cwd), str(self.options.session_id))
            for index in range(2):
                response = asyncio.get_running_loop().create_future()
                tool_use_id = "tool-normal-{}".format(index + 1)
                tool_input = {"path": "template-{}.yaml".format(index + 1)}
                assistant = Message(
                    role="assistant",
                    content=[ToolUseBlock(id=tool_use_id, name="write_file", input=tool_input)],
                )
                storage.append(str(self.options.cwd), str(self.options.session_id), assistant)
                yield PermissionRequestEvent(
                    tool_name="write_file",
                    tool_input=tool_input,
                    tool_use_id=tool_use_id,
                    response_future=response,
                    continuation_frame={
                        "assistantMessageRef": "session.jsonl:{}".format(index),
                        "assistantMessageDigest": canonical_digest(
                            [block.model_dump(mode="json") for block in assistant.content]
                        ),
                        "orderedToolUseIds": [tool_use_id],
                        "currentIndex": 0,
                        "decisions": [
                            {"toolUseId": tool_use_id, "state": "pending", "source": None, "deniedResult": None}
                        ],
                    },
                    audit_context={"session_id": str(self.options.session_id), "cwd": str(self.options.cwd)},
                )
                decisions.put((tool_use_id, response.result()))
            yield TextDeltaEvent(text="both permissions resolved")

    def runtime_factory(options):
        return SimpleNamespace(agent_loop=ConsecutivePermissionLoop(options), session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime_factory)
    monkeypatch.setattr("iac_code.a2a.input_required.emit_permission_boundary_audit", lambda *_a, **_k: True)

    a2a_port = _free_port()
    app = create_app(
        host="127.0.0.1",
        port=a2a_port,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "a2a-state",
        artifact_dir=tmp_path / "artifacts",
    )
    a2a_server, a2a_thread = _start_uvicorn(app, a2a_port)
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:{}/".format(a2a_port),
        workspace=str(workspace),
        ssl_context=_tls_context(tmp_path),
    )
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-start-chat-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])

    try:
        stdout, stderr = _aliyun_start_chat(aliyun, endpoint, "request consecutive permissions")
        result = _summarize_sse(stdout, stderr)
        assert result["state"] == "input-required"
        assert result["inputRequired"]["toolUseId"] == "tool-normal-1"

        stdout, stderr = _aliyun_start_chat(
            aliyun,
            endpoint,
            _permission_response_query(result["inputRequired"], "allow_once"),
            session_id=result["sessionId"],
        )
        second = _summarize_sse(stdout, stderr, session_id=result["sessionId"])
        assert second["state"] == "input-required"
        assert second["inputRequired"]["toolUseId"] == "tool-normal-2"

        stdout, stderr = _aliyun_start_chat(
            aliyun,
            endpoint,
            _permission_response_query(second["inputRequired"], "allow_once"),
            session_id=result["sessionId"],
        )
        third = _summarize_sse(stdout, stderr, session_id=result["sessionId"])
        assert third["state"] == "turn-completed"
        assert third["finalText"] == "both permissions resolved"
        assert decisions.get(timeout=2) == ("tool-normal-1", True)
        assert decisions.get(timeout=2) == ("tool-normal-2", True)
    finally:
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)
        a2a_server.should_exit = True
        a2a_thread.join(timeout=10)


def test_top_pipeline_permission_ends_parent_start_chat_and_continues_on_reply_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    aliyun = shutil.which("aliyun")
    if aliyun is None:
        pytest.skip("Alibaba Cloud CLI is not installed")
    from iac_code.a2a import executor as executor_module
    from iac_code.a2a import pipeline_executor as pipeline_executor_module
    from scripts.a2a.e2e.permission_wait.permission_wait_fixture_server import (
        _create_fixture_pipeline,
        _create_fixture_runtime,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    execution_log = tmp_path / "tool-executions.log"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    monkeypatch.setenv("IACCODE_A2A_ALLOWED_CWDS", str(workspace))

    def runtime_factory(options):
        return _create_fixture_runtime(options, execution_log=execution_log)

    monkeypatch.setattr(executor_module, "create_agent_runtime", runtime_factory)
    monkeypatch.setattr(pipeline_executor_module, "create_agent_runtime", runtime_factory)
    monkeypatch.setattr(
        pipeline_executor_module,
        "create_pipeline",
        lambda *unused_args, **kwargs: _create_fixture_pipeline(execution_log=execution_log, **kwargs),
    )

    a2a_port = _free_port()
    app = create_app(
        host="127.0.0.1",
        port=a2a_port,
        token=None,
        model="permission-wait-fixture",
        persistence_dir=tmp_path / "a2a-state",
        artifact_dir=tmp_path / "artifacts",
        auto_approve_permissions=False,
        permission_wait={
            "resident_timeout_seconds": None,
            "sub_pipeline_timeout_seconds": None,
            "timeout_grace_seconds": 30,
        },
    )
    a2a_server, a2a_thread = _start_uvicorn(app, a2a_port)
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:{}/".format(a2a_port),
        workspace=str(workspace),
        ssl_context=_tls_context(tmp_path),
        heartbeat_interval=0.05,
    )
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-start-chat-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])
    parent_process = None

    try:
        parent_process = subprocess.Popen(
            _aliyun_start_chat_command(aliyun, endpoint, "request top pipeline permission", mode="pipeline"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + 10
        checkpoint = None
        session = None
        while time.monotonic() < deadline:
            matches = sorted((tmp_path / "config").rglob("permission-waits/pwb_*.json"))
            if matches:
                checkpoint = json.loads(matches[0].read_text(encoding="utf-8"))
                with relay_server.sessions_lock:
                    sessions = list(relay_server.sessions.values())
                session = sessions[0] if sessions else None
                if session is not None:
                    break
            assert parent_process.poll() is None
            time.sleep(0.02)
        assert checkpoint is not None
        assert session is not None
        assert checkpoint["permissionClass"] == "pipeline"
        parent_stdout, parent_stderr = parent_process.communicate(timeout=10)
        parent = _summarize_sse(parent_stdout, parent_stderr, session_id=session.session_id, mode="pipeline")
        assert parent_process.returncode == 0
        assert parent.get("inputRequired", {}).get("kind") == "permission"
        assert '"eventType":"step_completed"' not in parent_stdout
        assert '"eventType":"pipeline_completed"' not in parent_stdout
        with session.state_lock:
            assert session.active_call is None

        permission = {
            "requestTaskId": checkpoint["taskId"],
            "contextId": checkpoint["contextId"],
            "inputId": checkpoint["inputId"],
            "toolUseId": checkpoint["toolUseId"],
        }
        reply_stdout, reply_stderr = _aliyun_start_chat(
            aliyun,
            endpoint,
            _permission_response_query(permission, "allow_once"),
            session_id=session.session_id,
            mode="pipeline",
        )
        reply = _summarize_sse(reply_stdout, reply_stderr, session_id=session.session_id, mode="pipeline")
        assert '"inputReceived"' in reply_stdout, (reply, bridge.sanitize_text(reply_stdout + reply_stderr, 2000))
        assert '"eventType":"step_completed"' in reply_stdout
        assert '"eventType":"pipeline_completed"' in reply_stdout
        assert execution_log.read_text(encoding="utf-8").splitlines() == ["executed"]
        resolved_checkpoint = json.loads(matches[0].read_text(encoding="utf-8"))
        assert resolved_checkpoint["phase"] == "RESOLVED"
        assert "continuationFrame" not in resolved_checkpoint
    finally:
        if parent_process is not None and parent_process.poll() is None:
            parent_process.terminate()
            parent_process.wait(timeout=5)
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)
        a2a_server.should_exit = True
        a2a_thread.join(timeout=10)


def test_sub_pipeline_permissions_round_trip_through_real_aliyun_cli_and_a2a(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    aliyun = shutil.which("aliyun")
    if aliyun is None:
        pytest.skip("Alibaba Cloud CLI is not installed")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("IAC_CODE_MODE", raising=False)
    outcomes: queue.Queue[list[bool]] = queue.Queue()
    prompts: queue.Queue[str] = queue.Queue()

    class SidebandSetup:
        def __init__(self, events: list[SubPipelineStreamEvent]) -> None:
            self.events = events

    class SidebandLoop:
        async def run_streaming(self, prompt: str):
            prompts.put(prompt)
            futures = [asyncio.get_running_loop().create_future() for _ in range(2)]
            events = [
                SubPipelineStreamEvent(
                    sub_pipeline_id="candidate-{}".format(index),
                    candidate_index=index,
                    inner=PermissionRequestEvent(
                        tool_name="bash",
                        tool_input={"cmd": "echo candidate-{}".format(index)},
                        tool_use_id="tool-sideband-{}".format(index),
                        response_future=future,
                    ),
                )
                for index, future in enumerate(futures)
            ]
            yield SidebandSetup([events[0]])
            await asyncio.sleep(0.25)
            yield SidebandSetup([events[1]])
            outcomes.put(list(await asyncio.gather(*futures)))
            yield TextDeltaEvent(text="sub pipeline permissions resolved")

    def runtime_factory(options):
        return SimpleNamespace(agent_loop=SidebandLoop(), session_id=options.session_id)

    monkeypatch.setattr("iac_code.a2a.executor.create_agent_runtime", runtime_factory)
    components = create_runtime_components(
        model="qwen3.6-plus",
        host="127.0.0.1",
        port=0,
        persistence_dir=tmp_path / "a2a-state",
        artifact_dir=tmp_path / "artifacts",
    )
    publisher_holder: dict[str, PipelineA2AEventPublisher] = {}

    async def publish_with_sideband(
        event_queue,
        *,
        task_id,
        context_id,
        event,
        permission_input_registry=None,
        **kwargs,
    ):
        if not isinstance(event, SidebandSetup):
            return await publish_stream_event_default(
                event_queue,
                task_id=task_id,
                context_id=context_id,
                event=event,
                permission_input_registry=permission_input_registry,
                **kwargs,
            )
        publisher = PipelineA2AEventPublisher(
            event_queue=event_queue,
            translator=PipelineEventTranslator(
                PipelineA2AContext(
                    pipeline_run_id=context_id,
                    task_id=task_id,
                    context_id=context_id,
                    pipeline_name="selling",
                )
            ),
            journal=A2APipelineJournal(tmp_path / "pipeline-sideband"),
            snapshot_store=A2APipelineSnapshotStore(tmp_path / "pipeline-sideband"),
            permission_input_registry=permission_input_registry,
            task_store=components.task_store,
        )
        publisher_holder["publisher"] = publisher
        for sideband_event in event.events:
            await publisher.publish_sub_pipeline_permission(sideband_event)
        return None

    monkeypatch.setattr("iac_code.a2a.executor.publish_stream_event", publish_with_sideband)
    monkeypatch.setattr("iac_code.a2a.pipeline_stream.emit_permission_boundary_audit", lambda *_a, **_k: True)
    monkeypatch.setattr("iac_code.a2a.transports.dispatcher.create_runtime_components", lambda **_kwargs: components)

    a2a_port = _free_port()
    app = create_app(
        host="127.0.0.1",
        port=a2a_port,
        token=None,
        model="qwen3.6-plus",
        persistence_dir=tmp_path / "unused-state",
        artifact_dir=tmp_path / "unused-artifacts",
    )
    a2a_server, a2a_thread = _start_uvicorn(app, a2a_port)
    relay_server = relay.StartChatRelay(
        ("127.0.0.1", 0),
        a2a_url="http://127.0.0.1:{}/".format(a2a_port),
        workspace=str(workspace),
        ssl_context=_tls_context(tmp_path),
    )
    relay_thread = threading.Thread(target=relay_server.serve_forever, name="test-start-chat-relay", daemon=True)
    relay_thread.start()
    endpoint = "127.0.0.1:{}".format(relay_server.server_address[1])
    parent_process = None

    try:
        parent_process = subprocess.Popen(
            _aliyun_start_chat_command(
                aliyun,
                endpoint,
                "start pipeline candidates",
                mode="pipeline",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + 10
        session = None
        first_permission = None
        while time.monotonic() < deadline:
            with relay_server.sessions_lock:
                sessions = list(relay_server.sessions.values())
            if sessions:
                session = sessions[0]
                with session.state_lock:
                    pending = list(session.pending_sideband.values())
                if pending:
                    first_permission = pending[0]
                    break
            assert parent_process.poll() is None
            time.sleep(0.02)
        assert session is not None
        assert first_permission is not None
        assert parent_process.poll() is None
        assert first_permission["kind"] == "permission"
        assert publisher_holder["publisher"] is not None
        with session.state_lock:
            parent_call = session.active_call
        assert parent_call is not None

        stdout, stderr = _aliyun_start_chat(
            aliyun,
            endpoint,
            _permission_response_query(first_permission, "allow_once"),
            session_id=session.session_id,
            mode="pipeline",
        )
        first_ack = _summarize_sse(stdout, stderr, session_id=session.session_id, mode="pipeline")
        assert first_ack["state"] == "permission-responded"
        assert first_ack["permissionAck"]["accepted"] is True
        assert first_ack["permissionAck"]["inputId"] == first_permission["inputId"]
        assert parent_process.poll() is None
        with session.state_lock:
            assert session.active_call is parent_call

        deadline = time.monotonic() + 10
        second_permission = None
        while time.monotonic() < deadline:
            with session.state_lock:
                remaining = list(session.pending_sideband.values())
            second_permission = next(
                (item for item in remaining if item.get("inputId") != first_permission["inputId"]),
                None,
            )
            if second_permission is not None:
                break
            time.sleep(0.02)
        assert second_permission is not None
        assert second_permission["inputId"] != first_permission["inputId"]

        stdout, stderr = _aliyun_start_chat(
            aliyun,
            endpoint,
            _permission_response_query(second_permission, "deny"),
            session_id=session.session_id,
            mode="pipeline",
        )
        second_ack = _summarize_sse(stdout, stderr, session_id=session.session_id, mode="pipeline")
        assert second_ack["state"] == "permission-responded"
        assert second_ack["permissionAck"]["accepted"] is True
        assert second_ack["permissionAck"]["inputId"] == second_permission["inputId"]

        parent_stdout, parent_stderr = parent_process.communicate(timeout=20)
        final = _summarize_sse(parent_stdout, parent_stderr, session_id=session.session_id, mode="pipeline")
        assert parent_process.returncode == 0
        assert final["state"] in {"turn-completed", "input-required"}
        assert final.get("finalText", final.get("latestText")) == "sub pipeline permissions resolved"
        assert "inputRequired" not in final
        assert parent_call.thread is not None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with session.state_lock:
                if session.active_call is None:
                    break
            time.sleep(0.01)
        with session.state_lock:
            assert session.active_call is None
            assert not session.pending_sideband

        assert outcomes.get(timeout=3) == [True, False]
        assert prompts.get(timeout=2) == "start pipeline candidates"
    finally:
        if parent_process is not None and parent_process.poll() is None:
            parent_process.terminate()
            parent_process.wait(timeout=5)
        relay_server.shutdown()
        relay_server.server_close()
        relay_thread.join(timeout=5)
        a2a_server.should_exit = True
        a2a_thread.join(timeout=10)
