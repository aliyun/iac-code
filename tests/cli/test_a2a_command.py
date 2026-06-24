import re
from types import SimpleNamespace

from typer.testing import CliRunner

from iac_code.a2a.persistence import A2APersistenceStore, A2ARouteSnapshot
from iac_code.a2a.transport import A2AAuthConfig
from iac_code.cli.main import (
    _a2a_client_missing_dependencies_message,
    _a2a_server_missing_dependencies_message,
    app,
)
from iac_code.config import DEFAULT_MODEL

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def test_a2a_help_shows_common_server_options_only() -> None:
    result = CliRunner().invoke(app, ["a2a", "--help"])

    assert result.exit_code == 0
    stdout = _strip_ansi(result.stdout)
    assert "--config" in stdout
    assert "--host" in stdout
    assert "--port" in stdout
    assert "--transport" in stdout
    assert "--thinking-exposure" in stdout
    assert "--debug" in stdout
    assert "--log-to-stdout" in stdout
    assert "--socket-path" not in stdout
    assert "--token" not in stdout
    assert "--persistence-dir" not in stdout
    assert "--push-redis-url" not in stdout
    assert "--auto-approve-permissions" not in stdout


def test_a2a_command_rejects_removed_advanced_flags() -> None:
    result = CliRunner().invoke(app, ["a2a", "--token", "cli-token"])

    assert result.exit_code == 2
    assert "No such option: --token" in result.stderr


def test_a2a_client_help_groups_client_commands() -> None:
    result = CliRunner().invoke(app, ["a2a-client", "--help"])

    assert result.exit_code == 0
    stdout = _strip_ansi(result.stdout)
    assert "call" in stdout
    assert "discover" in stdout
    assert "task-get" in stdout
    assert "push-config-create" in stdout
    assert "extended-card" in stdout
    assert "route-preview" in stdout
    assert "a2a-route-preview" not in stdout


def test_removed_top_level_a2a_client_commands_are_rejected() -> None:
    for command in ("a2a-call", "a2a-route-preview"):
        result = CliRunner().invoke(app, [command, "--help"])

        assert result.exit_code == 2
        assert "No such command" in result.stderr


def test_a2a_command_passes_config_options_to_server(monkeypatch, tmp_path) -> None:
    called = {}

    def fake_run_server(
        *,
        host: str,
        port: int,
        token: str | None,
        model: str,
        basic_username: str | None,
        basic_password: str | None,
        api_key: str | None,
        api_key_header: str,
        persistence_dir: str | None,
        artifact_dir: str | None,
        signing_secret: str | None,
        push_notifications: bool,
        transport: str,
        socket_path: str | None,
        ws_path: str,
        grpc_host: str | None,
        grpc_port: int | None,
        redis_url: str | None,
        request_stream: str,
        response_stream: str,
        consumer_group: str,
        push_queue: str,
        push_redis_url: str | None,
        push_stream: str,
        push_retry_key: str,
        push_dead_stream: str,
        push_consumer_group: str,
        push_consumer_name: str | None,
        push_lease_timeout_ms: int,
        auto_approve_permissions: bool,
        thinking_exposure: list[str] | None,
    ) -> None:
        called.update(
            {
                "host": host,
                "port": port,
                "token": token,
                "model": model,
                "basic_username": basic_username,
                "basic_password": basic_password,
                "api_key": api_key,
                "api_key_header": api_key_header,
                "persistence_dir": persistence_dir,
                "artifact_dir": artifact_dir,
                "signing_secret": signing_secret,
                "push_notifications": push_notifications,
                "transport": transport,
                "socket_path": socket_path,
                "ws_path": ws_path,
                "grpc_host": grpc_host,
                "grpc_port": grpc_port,
                "redis_url": redis_url,
                "request_stream": request_stream,
                "response_stream": response_stream,
                "consumer_group": consumer_group,
                "push_queue": push_queue,
                "push_redis_url": push_redis_url,
                "push_stream": push_stream,
                "push_retry_key": push_retry_key,
                "push_dead_stream": push_dead_stream,
                "push_consumer_group": push_consumer_group,
                "push_consumer_name": push_consumer_name,
                "push_lease_timeout_ms": push_lease_timeout_ms,
                "auto_approve_permissions": auto_approve_permissions,
                "thinking_exposure": thinking_exposure,
            }
        )

    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)
    monkeypatch.setattr("iac_code.a2a.app.resolve_token", lambda token: token or "env-token")
    monkeypatch.setattr("iac_code.a2a.app.resolve_basic_credentials", lambda username, password: (username, password))
    monkeypatch.setattr("iac_code.a2a.app.resolve_api_key", lambda api_key: api_key or "env-api-key")

    config = tmp_path / "a2a.yml"
    config.write_text(
        "\n".join(
            [
                "host: 0.0.0.0",
                "port: 9999",
                "token: cli-token",
                "basic-username: cli-user",
                "basic-password: cli-pass",
                "api-key: cli-api-key",
                "api-key-header: X-IAC-Code-Key",
                "persistence-dir: /tmp/a2a-persist",
                "artifact-dir: /tmp/a2a-artifacts",
                "signing-secret: sign-me",
                "push-notifications: true",
                "push-queue: redis-streams",
                "push-redis-url: redis://localhost:6379/0",
                "push-stream: custom:push",
                "push-retry-key: custom:push:retry",
                "push-dead-stream: custom:push:dead",
                "push-consumer-group: custom-workers",
                "push-consumer-name: worker-a",
                "push-lease-timeout-ms: 120000",
                "auto-approve-permissions: true",
                "thinking-exposure:",
                "  - raw-thinking",
                "  - tool-trace",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "a2a",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    assert called == {
        "host": "0.0.0.0",
        "port": 9999,
        "token": "cli-token",
        "model": DEFAULT_MODEL,
        "basic_username": "cli-user",
        "basic_password": "cli-pass",
        "api_key": "cli-api-key",
        "api_key_header": "X-IAC-Code-Key",
        "persistence_dir": "/tmp/a2a-persist",
        "artifact_dir": "/tmp/a2a-artifacts",
        "signing_secret": "sign-me",
        "push_notifications": True,
        "transport": "http",
        "socket_path": None,
        "ws_path": "/a2a",
        "grpc_host": None,
        "grpc_port": None,
        "redis_url": None,
        "request_stream": "iac-code:a2a:requests",
        "response_stream": "iac-code:a2a:responses",
        "consumer_group": "iac-code",
        "push_queue": "redis-streams",
        "push_redis_url": "redis://localhost:6379/0",
        "push_stream": "custom:push",
        "push_retry_key": "custom:push:retry",
        "push_dead_stream": "custom:push:dead",
        "push_consumer_group": "custom-workers",
        "push_consumer_name": "worker-a",
        "push_lease_timeout_ms": 120000,
        "auto_approve_permissions": True,
        "thinking_exposure": ["raw-thinking", "tool-trace"],
    }


def test_a2a_command_rejects_missing_push_redis_url(tmp_path) -> None:
    config = tmp_path / "a2a.yml"
    config.write_text("push-notifications: true\npush-queue: redis-streams\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["a2a", "--config", str(config)])

    assert result.exit_code == 1
    assert "push-redis-url is required in --config" in result.stderr


def test_a2a_command_loads_config_file_and_cli_overrides(monkeypatch, tmp_path) -> None:
    captured = {}
    logging_setup = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    def fake_setup_logging(**kwargs):
        logging_setup.update(kwargs)

    config = tmp_path / "a2a.yml"
    config.write_text(
        "\n".join(
            [
                "host: 0.0.0.0",
                "port: 12345",
                "transport: websocket",
                "ws_path: /agent",
                "token: config-token",
                "persistence_dir: /tmp/from-config",
                "push_notifications: true",
                "auto_approve_permissions: true",
                "thinking_exposure: raw-thinking, tool-trace",
                "log-to-stdout: true",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("iac_code.cli.main.load_saved_model", lambda: "qwen3.6-plus")
    monkeypatch.setattr("iac_code.cli.main.setup_logging", fake_setup_logging)
    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)

    result = CliRunner().invoke(app, ["a2a", "--config", str(config), "--port", "54321"])

    assert result.exit_code == 0
    assert logging_setup == {"session_id": "a2a-server", "debug": False, "stdout": True}
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 54321
    assert captured["transport"] == "websocket"
    assert captured["ws_path"] == "/agent"
    assert captured["token"] == "config-token"
    assert captured["persistence_dir"] == "/tmp/from-config"
    assert captured["push_notifications"] is True
    assert captured["auto_approve_permissions"] is True
    assert captured["thinking_exposure"] == "raw-thinking, tool-trace"


def test_a2a_command_log_to_stdout_cli_overrides_config(monkeypatch, tmp_path) -> None:
    logging_setup = {}

    def fake_setup_logging(**kwargs):
        logging_setup.update(kwargs)

    config = tmp_path / "a2a.yml"
    config.write_text("log-to-stdout: false\n", encoding="utf-8")
    monkeypatch.setattr("iac_code.cli.main.setup_logging", fake_setup_logging)
    monkeypatch.setattr("iac_code.a2a.app.run_server", lambda **_kwargs: None)

    result = CliRunner().invoke(app, ["a2a", "--config", str(config), "--log-to-stdout"])

    assert result.exit_code == 0
    assert logging_setup == {"session_id": "a2a-server", "debug": False, "stdout": True}


def test_a2a_command_no_log_to_stdout_cli_overrides_config(monkeypatch, tmp_path) -> None:
    logging_setup = {}

    def fake_setup_logging(**kwargs):
        logging_setup.update(kwargs)

    config = tmp_path / "a2a.yml"
    config.write_text("log-to-stdout: true\n", encoding="utf-8")
    monkeypatch.setattr("iac_code.cli.main.setup_logging", fake_setup_logging)
    monkeypatch.setattr("iac_code.a2a.app.run_server", lambda **_kwargs: None)

    result = CliRunner().invoke(app, ["a2a", "--config", str(config), "--no-log-to-stdout"])

    assert result.exit_code == 0
    assert logging_setup == {"session_id": "a2a-server", "debug": False, "stdout": False}


def test_a2a_command_rejects_stdio_log_to_stdout(monkeypatch) -> None:
    monkeypatch.setattr("iac_code.cli.main.setup_logging", lambda **_kwargs: None)

    result = CliRunner().invoke(app, ["a2a", "--transport", "stdio", "--log-to-stdout"])

    assert result.exit_code == 1
    assert "--log-to-stdout cannot be used with --transport stdio" in result.stderr


def test_a2a_command_accepts_repeated_thinking_exposure_flags(monkeypatch) -> None:
    captured = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)

    result = CliRunner().invoke(
        app,
        [
            "a2a",
            "--thinking-exposure",
            "raw-thinking",
            "--thinking-exposure",
            "tool-trace",
        ],
    )

    assert result.exit_code == 0
    assert captured["thinking_exposure"] == ["raw-thinking", "tool-trace"]


def test_a2a_command_reports_invalid_thinking_exposure(tmp_path) -> None:
    config = tmp_path / "a2a.yml"
    config.write_text("thinking-exposure: thought-summary\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["a2a", "--config", str(config)])

    assert result.exit_code == 1
    assert "Unsupported A2A thinking exposure type 'thought_summary'" in result.stderr


def test_a2a_command_passes_unix_transport_options(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("iac_code.cli.main.load_saved_model", lambda: "qwen3.6-plus")
    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)
    config = tmp_path / "a2a.yml"
    config.write_text("socket-path: /tmp/iac-code.sock\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["a2a", "--config", str(config), "--transport", "unix"])

    assert result.exit_code == 0
    assert captured["transport"] == "unix"
    assert captured["socket_path"] == "/tmp/iac-code.sock"


def test_a2a_command_preserves_explicit_zero_grpc_port(monkeypatch, tmp_path) -> None:
    captured = {}

    def fake_run_server(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("iac_code.cli.main.load_saved_model", lambda: "qwen3.6-plus")
    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)
    config = tmp_path / "a2a.yml"
    config.write_text("grpc-port: 0\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["a2a", "--config", str(config)])

    assert result.exit_code == 0
    assert captured["grpc_port"] == 0


def test_a2a_command_rejects_missing_socket_path() -> None:
    result = CliRunner().invoke(app, ["a2a", "--transport", "unix"])

    assert result.exit_code == 1
    assert "socket-path is required in --config" in result.stderr


def test_a2a_call_sends_prompt_with_auth(monkeypatch, tmp_path) -> None:
    called = {}

    class FakeClient:
        def __init__(
            self,
            *,
            auth: A2AAuthConfig | None = None,
            verification_secret: str | None = None,
            verification_jwks_url: str | None = None,
            require_card_signature: bool = False,
            timeout_seconds: float | None = None,
        ) -> None:
            called["auth"] = auth
            called["verification_secret"] = verification_secret
            called["verification_jwks_url"] = verification_jwks_url
            called["require_card_signature"] = require_card_signature
            called["timeout_seconds"] = timeout_seconds

        async def send_message(
            self,
            url: str,
            prompt: str,
            *,
            cwd: str,
            context_id: str | None = None,
            model: str | None = None,
            iac_code_api_key: str | None = None,
        ):
            called["send"] = {
                "url": url,
                "prompt": prompt,
                "cwd": cwd,
                "context_id": context_id,
                "model": model,
                "iac_code_api_key": iac_code_api_key,
            }
            return SimpleNamespace(text="created stack", payload={"result": {"text": "created stack"}})

        async def discover(self, url: str):
            called["discover"] = url
            return {
                "name": "iac-agent",
                "supportedInterfaces": [
                    {
                        "url": "http://agent.example/discovered-rpc",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
            }

        @staticmethod
        def select_endpoint_url(card, *, fallback_url: str) -> str:
            called["selected_card"] = card
            called["fallback_url"] = fallback_url
            return card["supportedInterfaces"][0]["url"]

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "call",
            "--url",
            "http://agent.example/rpc",
            "--prompt",
            "create vpc",
            "--cwd",
            str(tmp_path),
            "--context-id",
            "ctx-1",
            "--iac-code-model",
            "metadata-model",
            "--iac-code-api-key",
            "metadata-api-key",
            "--token",
            "bearer",
            "--api-key",
            "api",
            "--api-key-header",
            "X-IAC-Code-Key",
            "--basic-username",
            "user",
            "--basic-password",
            "pass",
            "--verify-card-secret",
            "card-secret",
            "--verify-card-jwks-url",
            "https://agent.example/.well-known/jwks.json",
            "--require-card-signature",
            "--timeout",
            "12.5",
        ],
    )

    assert result.exit_code == 0
    assert "created stack" in result.output
    assert called["send"] == {
        "url": "http://agent.example/discovered-rpc",
        "prompt": "create vpc",
        "cwd": str(tmp_path),
        "context_id": "ctx-1",
        "model": "metadata-model",
        "iac_code_api_key": "metadata-api-key",
    }
    assert called["discover"] == "http://agent.example/rpc"
    assert called["fallback_url"] == "http://agent.example/rpc"
    assert called["auth"] == A2AAuthConfig(
        bearer_token="bearer",
        api_key="api",
        api_key_header="X-IAC-Code-Key",
        basic_username="user",
        basic_password="pass",
    )
    assert called["verification_secret"] == "card-secret"
    assert called["verification_jwks_url"] == "https://agent.example/.well-known/jwks.json"
    assert called["require_card_signature"] is True
    assert called["timeout_seconds"] == 12.5
    assert called["closed"] is True


def test_a2a_call_default_cwd_prefers_logical_pwd(monkeypatch, tmp_path) -> None:
    called = {}
    physical = tmp_path / "mount-root" / "oss" / "bucket"
    physical.mkdir(parents=True)
    logical = tmp_path / "workspace"
    logical.symlink_to(physical, target_is_directory=True)

    async def fake_run_a2a_call(**kwargs) -> str:
        called.update(kwargs)
        return "ok"

    monkeypatch.chdir(logical)
    monkeypatch.setenv("PWD", str(logical))
    monkeypatch.setattr("iac_code.cli.main._run_a2a_call", fake_run_a2a_call)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "call",
            "--url",
            "http://agent.example/rpc",
            "--prompt",
            "create vpc",
        ],
    )

    assert result.exit_code == 0
    assert called["cwd"] == str(logical)


def test_a2a_call_stream_prints_stream_events(monkeypatch, tmp_path) -> None:
    called = {}

    class FakeClient:
        def __init__(self, *, auth: A2AAuthConfig | None = None, **_kwargs) -> None:
            called["auth"] = auth

        async def discover(self, url: str):
            called["discover"] = url
            return {"url": "http://agent.example/rpc"}

        @staticmethod
        def select_endpoint_url(card, *, fallback_url: str) -> str:
            return card.get("url", fallback_url)

        async def stream_message(
            self,
            url: str,
            prompt: str,
            *,
            cwd: str,
            context_id: str | None = None,
            model: str | None = None,
            iac_code_api_key: str | None = None,
        ):
            called["stream"] = {
                "url": url,
                "prompt": prompt,
                "cwd": cwd,
                "context_id": context_id,
                "model": model,
                "iac_code_api_key": iac_code_api_key,
            }
            yield {"result": {"status": {"state": "working", "message": {"parts": [{"text": "planning"}]}}}}
            yield {"result": {"text": "created stack"}}

        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("stream mode must not call send_message")

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "call",
            "--url",
            "http://agent.example",
            "--prompt",
            "create vpc",
            "--cwd",
            str(tmp_path),
            "--context-id",
            "ctx-1",
            "--stream",
        ],
    )

    assert result.exit_code == 0
    assert "planning" in result.output
    assert "created stack" in result.output
    assert called["stream"] == {
        "url": "http://agent.example/rpc",
        "prompt": "create vpc",
        "cwd": str(tmp_path),
        "context_id": "ctx-1",
        "model": None,
        "iac_code_api_key": None,
    }
    assert called["closed"] is True


def test_a2a_call_can_resolve_named_route(monkeypatch, tmp_path) -> None:
    called = {}

    async def fake_run_a2a_call(**kwargs) -> str:
        called.update(kwargs)
        return "ok"

    monkeypatch.setattr("iac_code.cli.main._run_a2a_call", fake_run_a2a_call)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "call",
            "--route",
            "template=http://template.example/rpc;iac_generation;ros",
            "--route-name",
            "template",
            "--prompt",
            "create vpc",
            "--cwd",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert called["url"] == "http://template.example/rpc"
    assert called["prompt"] == "create vpc"


def test_a2a_client_call_loads_config_and_allows_cli_overrides(monkeypatch, tmp_path) -> None:
    called = {}

    async def fake_run_a2a_call(**kwargs) -> str:
        called.update(kwargs)
        return "ok"

    monkeypatch.setattr("iac_code.cli.main._run_a2a_call", fake_run_a2a_call)

    config = tmp_path / "a2a-client.yml"
    config.write_text(
        "\n".join(
            [
                "url: http://agent.example/rpc",
                "cwd: /workspace/from-config",
                "context-id: ctx-from-config",
                "iac-code-model: config-model",
                "iac-code-api-key: config-iac-code-api-key",
                "token: config-token",
                "basic-username: config-user",
                "basic-password: config-pass",
                "api-key: config-api",
                "api-key-header: X-IAC-Code-Key",
                "verify-card-secret: config-secret",
                "verify-card-jwks-url: https://agent.example/.well-known/jwks.json",
                "require-card-signature: true",
                "timeout: 20",
                "stream: true",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "--config",
            str(config),
            "call",
            "--prompt",
            "create vpc",
            "--timeout",
            "12.5",
        ],
    )

    assert result.exit_code == 0
    stream_callback = called.pop("stream_callback")
    assert called == {
        "url": "http://agent.example/rpc",
        "prompt": "create vpc",
        "cwd": "/workspace/from-config",
        "context_id": "ctx-from-config",
        "model": "config-model",
        "iac_code_api_key": "config-iac-code-api-key",
        "token": "config-token",
        "basic_username": "config-user",
        "basic_password": "config-pass",
        "api_key": "config-api",
        "api_key_header": "X-IAC-Code-Key",
        "verify_card_secret": "config-secret",
        "verify_card_jwks_url": "https://agent.example/.well-known/jwks.json",
        "require_card_signature": True,
        "timeout_seconds": 12.5,
        "stream": True,
    }
    assert stream_callback is not None


def test_a2a_client_call_loads_routes_from_config(monkeypatch, tmp_path) -> None:
    called = {}

    async def fake_run_a2a_call(**kwargs) -> str:
        called.update(kwargs)
        return "ok"

    monkeypatch.setattr("iac_code.cli.main._run_a2a_call", fake_run_a2a_call)

    config = tmp_path / "a2a-client.yml"
    config.write_text(
        "\n".join(
            [
                "route-name: template",
                "routes:",
                "  - name: template",
                "    url: http://template.example/rpc",
                "    skills:",
                "      - iac_generation",
                "    tags:",
                "      - ros",
                "      - template",
                "  - name: review",
                "    url: http://review.example/rpc",
                "    skills:",
                "      - iac_review",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "--config",
            str(config),
            "call",
            "--prompt",
            "create vpc",
        ],
    )

    assert result.exit_code == 0
    assert called["url"] == "http://template.example/rpc"
    assert called["prompt"] == "create vpc"


def test_a2a_client_task_get_loads_url_and_task_id_from_config(monkeypatch, tmp_path) -> None:
    called = {}

    class FakeClient:
        def __init__(self, *, auth: A2AAuthConfig | None = None) -> None:
            called["auth"] = auth

        async def get_task(self, url: str, task_id: str, *, history_length: int | None = None):
            called["get_task"] = {"url": url, "task_id": task_id, "history_length": history_length}
            return {"result": {"id": task_id}}

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    config = tmp_path / "a2a-client.yml"
    config.write_text(
        "\n".join(
            [
                "url: http://agent.example/rpc",
                "task-id: task-from-config",
                "history-length: 5",
                "token: config-token",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["a2a-client", "--config", str(config), "task-get"])

    assert result.exit_code == 0
    assert '"id": "task-from-config"' in result.output
    assert called["get_task"] == {
        "url": "http://agent.example/rpc",
        "task_id": "task-from-config",
        "history_length": 5,
    }
    assert called["auth"] == A2AAuthConfig(bearer_token="config-token")
    assert called["closed"] is True


def test_a2a_client_task_list_reports_missing_url_without_config() -> None:
    result = CliRunner().invoke(app, ["a2a-client", "task-list"])

    assert result.exit_code == 1
    assert "url is required. Provide --url or url in --config." in result.stderr


def test_a2a_discover_prints_agent_card(monkeypatch) -> None:
    called = {}

    class FakeClient:
        def __init__(
            self,
            *,
            auth: A2AAuthConfig | None = None,
            verification_secret: str | None = None,
            verification_jwks_url: str | None = None,
            require_card_signature: bool = False,
        ) -> None:
            called["auth"] = auth
            called["verification_secret"] = verification_secret
            called["verification_jwks_url"] = verification_jwks_url
            called["require_card_signature"] = require_card_signature

        async def discover(self, base_url: str):
            called["base_url"] = base_url
            return {"name": "iac-agent", "skills": [{"id": "iac_generation"}]}

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "discover",
            "--url",
            "http://agent.example",
            "--token",
            "bearer",
            "--verify-card-secret",
            "card-secret",
            "--verify-card-jwks-url",
            "https://agent.example/.well-known/jwks.json",
            "--require-card-signature",
        ],
    )

    assert result.exit_code == 0
    assert '"name": "iac-agent"' in result.output
    assert called == {
        "auth": A2AAuthConfig(bearer_token="bearer"),
        "verification_secret": "card-secret",
        "verification_jwks_url": "https://agent.example/.well-known/jwks.json",
        "require_card_signature": True,
        "base_url": "http://agent.example",
        "closed": True,
    }


def test_a2a_task_get_calls_client(monkeypatch) -> None:
    called = {}

    class FakeClient:
        def __init__(self, *, auth: A2AAuthConfig | None = None) -> None:
            called["auth"] = auth

        async def get_task(self, url: str, task_id: str, *, history_length: int | None = None):
            called["get_task"] = {"url": url, "task_id": task_id, "history_length": history_length}
            return {"result": {"id": task_id}}

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "task-get",
            "--url",
            "http://agent.example/rpc",
            "--task-id",
            "task-1",
            "--history-length",
            "3",
            "--token",
            "bearer",
        ],
    )

    assert result.exit_code == 0
    assert '"id": "task-1"' in result.output
    assert called["get_task"] == {"url": "http://agent.example/rpc", "task_id": "task-1", "history_length": 3}
    assert called["auth"] == A2AAuthConfig(bearer_token="bearer")
    assert called["closed"] is True


def test_a2a_task_list_prints_table_with_pagination_hint(monkeypatch) -> None:
    called = {}

    class FakeClient:
        def __init__(self, *, auth: A2AAuthConfig | None = None) -> None:
            called["auth"] = auth

        async def list_tasks(self, url: str, **kwargs):
            called["list_tasks"] = {"url": url, **kwargs}
            return {
                "result": {
                    "tasks": [
                        {
                            "id": "task-1",
                            "contextId": "ctx-1",
                            "status": {
                                "state": "TASK_STATE_WORKING",
                                "timestamp": "2026-05-15T10:00:00Z",
                                "message": {"parts": [{"text": "creating vpc"}]},
                            },
                        },
                        {
                            "id": "task-2",
                            "contextId": "ctx-2",
                            "status": {
                                "state": "TASK_STATE_COMPLETED",
                                "timestamp": "2026-05-15T09:00:00Z",
                            },
                        },
                    ],
                    "nextPageToken": "cursor-2",
                    "pageSize": 2,
                    "totalSize": 3,
                }
            }

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "task-list",
            "--url",
            "http://agent.example/rpc",
            "--context-id",
            "ctx-1",
            "--status",
            "TASK_STATE_WORKING",
            "--page-size",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "ID" in result.output
    assert "Status" in result.output
    assert "task-1" in result.output
    assert "working" in result.output
    assert "creating vpc" in result.output
    assert "Showing 2 of 3 tasks" in result.output
    assert "iac-code a2a-client task-list" in result.output
    assert "--page-token cursor-2" in result.output
    assert called["list_tasks"] == {
        "url": "http://agent.example/rpc",
        "context_id": "ctx-1",
        "status": "TASK_STATE_WORKING",
        "page_size": 2,
        "page_token": None,
        "include_artifacts": None,
    }
    assert called["closed"] is True


def test_a2a_task_list_can_print_json(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *, auth: A2AAuthConfig | None = None) -> None:
            pass

        async def list_tasks(self, url: str, **_kwargs):
            return {"result": {"tasks": [{"id": "task-1"}], "totalSize": 1}}

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "task-list",
            "--url",
            "http://agent.example/rpc",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert '"tasks"' in result.output
    assert "Next page" not in result.output


def test_a2a_push_config_create_calls_client(monkeypatch) -> None:
    called = {}

    class FakeClient:
        def __init__(self, *, auth: A2AAuthConfig | None = None) -> None:
            called["auth"] = auth

        async def create_push_notification_config(self, **kwargs):
            called["create"] = kwargs
            return {"result": {"id": kwargs["config_id"], "url": kwargs["url"]}}

        async def aclose(self) -> None:
            called["closed"] = True

    monkeypatch.setattr("iac_code.a2a.client.A2AClient", FakeClient)

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "push-config-create",
            "--url",
            "http://agent.example/rpc",
            "--task-id",
            "task-1",
            "--config-id",
            "cfg-1",
            "--callback-url",
            "https://callback.example/a2a",
            "--notification-token",
            "token-1",
            "--auth-scheme",
            "bearer",
            "--auth-credentials",
            "secret",
        ],
    )

    assert result.exit_code == 0
    assert '"id": "cfg-1"' in result.output
    assert called["create"]["config_id"] == "cfg-1"
    assert called["create"]["authentication"] == {"scheme": "bearer", "credentials": "secret"}


def test_a2a_route_preview_resolves_and_saves_routes(tmp_path) -> None:
    persistence_dir = tmp_path / "a2a"

    result = CliRunner().invoke(
        app,
        [
            "a2a-client",
            "route-preview",
            "--route",
            "template=http://template.example/rpc;skills=iac_generation;tags=ros,template",
            "--route",
            "review=http://review.example/rpc;skills=iac_review;tags=review",
            "--skill",
            "iac_generation",
            "--route-state-dir",
            str(persistence_dir),
        ],
    )

    assert result.exit_code == 0
    assert "template" in result.output
    assert "http://template.example/rpc" in result.output
    assert A2APersistenceStore(persistence_dir).load_routes() == [
        A2ARouteSnapshot(
            name="template",
            url="http://template.example/rpc",
            skills=["iac_generation"],
            tags=["ros", "template"],
        ),
        A2ARouteSnapshot(name="review", url="http://review.example/rpc", skills=["iac_review"], tags=["review"]),
    ]


def test_a2a_command_reports_missing_extra(monkeypatch) -> None:
    def fake_run_server(**kwargs) -> None:
        raise RuntimeError(_a2a_server_missing_dependencies_message())

    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)
    monkeypatch.setattr("iac_code.a2a.app.resolve_token", lambda token: None)
    monkeypatch.setattr("iac_code.a2a.app.resolve_basic_credentials", lambda username, password: None)
    monkeypatch.setattr("iac_code.a2a.app.resolve_api_key", lambda api_key: None)

    result = CliRunner().invoke(app, ["a2a"])

    assert result.exit_code == 1
    combined_output = (result.stdout or "") + (result.stderr or "") + (result.output or "")
    assert _a2a_server_missing_dependencies_message() in combined_output


def test_a2a_command_reports_import_error(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "iac_code.a2a.app":
            raise ImportError("missing optional a2a dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = CliRunner().invoke(app, ["a2a"])

    assert result.exit_code == 1
    combined_output = (result.stdout or "") + (result.stderr or "") + (result.output or "")
    assert _a2a_server_missing_dependencies_message() in combined_output


def test_a2a_client_command_reports_import_error(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "iac_code.a2a.client":
            raise ImportError("missing optional a2a dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    result = CliRunner().invoke(app, ["a2a-client", "discover", "--url", "http://agent.example/rpc"])

    assert result.exit_code == 1
    combined_output = (result.stdout or "") + (result.stderr or "") + (result.output or "")
    assert _a2a_client_missing_dependencies_message() in combined_output


def test_a2a_command_bootstraps_telemetry_around_run_server(monkeypatch) -> None:
    boot_calls: list[str | None] = []
    events: list[tuple[str, dict]] = []
    metrics: list[tuple[str, int | float, dict]] = []
    shutdown_calls: list[int] = []
    server_kwargs: dict = {}

    def fake_bootstrap(session_id=None):
        boot_calls.append(session_id)

    def fake_log_event(name, payload=None):
        events.append((name, dict(payload or {})))

    def fake_add_metric(name, value, attributes=None):
        metrics.append((name, value, dict(attributes or {})))

    def fake_shutdown():
        shutdown_calls.append(1)
        # The finally block must still see telemetry bootstrapped when shutdown runs.
        assert boot_calls, "graceful_shutdown called before bootstrap_telemetry"

    def fake_run_server(**kwargs):
        server_kwargs.update(kwargs)
        # By the time run_server executes, telemetry must already be bootstrapped
        # and the SESSION_STARTED event must already be recorded — otherwise per-
        # call spans/events inside the agent loop would be dropped.
        assert len(boot_calls) == 1
        assert any(name == "iac.session.started" for name, _ in events)

    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", fake_bootstrap)
    monkeypatch.setattr("iac_code.services.telemetry.log_event", fake_log_event)
    monkeypatch.setattr("iac_code.services.telemetry.add_metric", fake_add_metric)
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", fake_shutdown)
    monkeypatch.setattr("iac_code.cli.main.load_saved_model", lambda: "qwen3.6-plus")
    monkeypatch.setattr("iac_code.a2a.app.run_server", fake_run_server)

    result = CliRunner().invoke(app, ["a2a", "--transport", "http"])

    assert result.exit_code == 0
    assert len(boot_calls) == 1
    assert boot_calls[0] is not None
    assert boot_calls[0].startswith("a2a-server-")

    started = [payload for name, payload in events if name == "iac.session.started"]
    assert started == [{"mode": "a2a-server", "transport": "http"}]

    exited = [payload for name, payload in events if name == "iac.session.exited"]
    assert len(exited) == 1
    assert exited[0]["mode"] == "a2a-server"
    assert exited[0]["reason"] == "normal"
    assert isinstance(exited[0]["duration_s"], int)

    assert ("iac.session.count", 1, {}) in metrics
    assert server_kwargs["transport"] == "http"
    assert shutdown_calls == [1]


def test_a2a_command_does_not_create_settings_yml_before_request(monkeypatch, tmp_path) -> None:
    from iac_code.services.telemetry import set_client

    config_dir = tmp_path / "config"
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    set_client(None)
    monkeypatch.setattr("iac_code.cli.main.load_saved_model", lambda: "qwen3.6-plus")
    monkeypatch.setattr("iac_code.a2a.app.run_server", lambda **kwargs: None)
    monkeypatch.setattr("atexit.register", lambda *args, **kwargs: None)

    try:
        result = CliRunner().invoke(app, ["a2a", "--transport", "http"])
    finally:
        set_client(None)

    assert result.exit_code == 0
    assert not (config_dir / "settings.yml").exists()


def test_a2a_command_flushes_telemetry_when_validation_fails(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    shutdown_calls: list[int] = []

    monkeypatch.setattr("iac_code.services.telemetry.bootstrap_telemetry", lambda session_id=None: None)
    monkeypatch.setattr(
        "iac_code.services.telemetry.log_event",
        lambda name, payload=None: events.append((name, dict(payload or {}))),
    )
    monkeypatch.setattr("iac_code.services.telemetry.add_metric", lambda *args, **kwargs: None)
    monkeypatch.setattr("iac_code.services.telemetry.graceful_shutdown", lambda: shutdown_calls.append(1))
    monkeypatch.setattr("iac_code.cli.main.load_saved_model", lambda: "qwen3.6-plus")

    # --transport unix without --config socket-path triggers the validation RuntimeError.
    result = CliRunner().invoke(app, ["a2a", "--transport", "unix"])

    assert result.exit_code == 1
    exited = [payload for name, payload in events if name == "iac.session.exited"]
    assert len(exited) == 1
    assert exited[0]["reason"] == "error"
    assert shutdown_calls == [1]
