from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "skills/alicloud-ros-agent/scripts/ros_agent.py"
BRIDGE_SOURCE_PATHS = (
    BRIDGE_PATH,
    BRIDGE_PATH.with_name("_ros_agent_core.py"),
    BRIDGE_PATH.with_name("_ros_agent_projection.py"),
    BRIDGE_PATH.with_name("_ros_agent_runtime.py"),
)


def _load_bridge():
    spec = importlib.util.spec_from_file_location("alicloud_ros_agent_skill_bridge", BRIDGE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


def _write_fake_aliyun(tmp_path: Path, source: str) -> Path:
    """Create a fake aliyun executable that also works with CreateProcess."""

    script = tmp_path / "fake_aliyun.py"
    script.write_text(source, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "aliyun.cmd"
        command = subprocess.list2cmdline([sys.executable, str(script)])
        launcher.write_text("@echo off\r\n{} %*\r\n".format(command), encoding="utf-8")
        return launcher
    launcher = tmp_path / "aliyun"
    launcher.write_text("#!{}\n{}".format(sys.executable, source), encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


class _FakeCLIProcess:
    def __init__(self, output: str, return_code: int = 0) -> None:
        self.stdout = io.StringIO(output)
        self.return_code = return_code

    def wait(self, timeout=None):
        del timeout
        return self.return_code

    def poll(self):
        return self.return_code

    def terminate(self):
        return None

    def kill(self):
        return None


def _clear_code_credential_env(monkeypatch) -> None:
    for name in (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABACLOUD_ACCESS_KEY_ID",
        "ALICLOUD_ACCESS_KEY_ID",
        "ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIBABACLOUD_ACCESS_KEY_SECRET",
        "ALICLOUD_ACCESS_KEY_SECRET",
        "ACCESS_KEY_SECRET",
        "ALIBABA_CLOUD_SECURITY_TOKEN",
        "ALIBABACLOUD_SECURITY_TOKEN",
        "ALICLOUD_SECURITY_TOKEN",
        "SECURITY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def _clear_region_and_profile_env(monkeypatch) -> None:
    for name in bridge.REGION_ENV_NAMES + bridge.PROFILE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _chat_args(**overrides):
    values = {
        "aliyun_path": "aliyun",
        "endpoint": "ros.aliyuncs.com",
        "connect_timeout": 10,
        "read_timeout": 600,
        "profile": None,
        "region_id": "cn-hangzhou",
        "no_thinking": False,
        "mode": "normal",
        "session_id": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _command_body(command):
    return json.loads(command[command.index("--body") + 1])


def _status_event(*, state="TASK_STATE_WORKING", text="", metadata=None):
    message = {"role": "ROLE_AGENT", "parts": [{"text": text}]} if text else None
    status = {"state": state}
    if message is not None:
        status["message"] = message
    return {
        "result": {
            "statusUpdate": {
                "taskId": "task-1",
                "contextId": "session-1",
                "status": status,
                "metadata": {"iac_code": metadata or {}, "iacCodeSessionId": "iac-session-1"},
            }
        }
    }


def test_bridge_parses_as_python_38_and_uses_only_standard_library_imports() -> None:
    sources = [path.read_text(encoding="utf-8") for path in BRIDGE_SOURCE_PATHS]
    trees = [
        ast.parse(source, filename=str(path), feature_version=(3, 8))
        for path, source in zip(BRIDGE_SOURCE_PATHS, sources)
    ]
    imported_modules = {
        alias.name.split(".", 1)[0]
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported_modules <= {
        "argparse",
        "contextlib",
        "ctypes",
        "errno",
        "fcntl",
        "hashlib",
        "http",
        "importlib",
        "json",
        "msvcrt",
        "os",
        "pathlib",
        "re",
        "secrets",
        "shutil",
        "socket",
        "ssl",
        "subprocess",
        "sys",
        "tempfile",
        "threading",
        "time",
        "typing",
        "urllib",
        "uuid",
    }
    combined_source = "\n".join(sources).lower()
    assert "access-key-id" not in combined_source
    assert "access-key-secret" not in combined_source
    assert all(path.stat().st_size <= 128 * 1024 for path in BRIDGE_SOURCE_PATHS)


def test_build_command_uses_ros_plugin_without_explicit_credentials(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    command = bridge.build_command(
        _chat_args(
            mode="pipeline",
            profile="skill-profile",
            session_id="session-1",
            no_thinking=True,
        ),
        "创建 VPC",
        None,
        [
            {
                "Type": "image",
                "MimeType": "image/png",
                "Name": "diagram.png",
                "OssObjectKey": "user/workspace/diagram.png",
            }
        ],
    )

    assert command[:3] == ["/usr/local/bin/aliyun", "ros", "start-chat"]
    assert "--force" not in command
    assert "--method" not in command
    assert command[command.index("--biz-mode") + 1] == "IaCCodePipeline"
    assert command[command.index("--session-id") + 1] == "session-1"
    assert command[command.index("--enable-partial-message") + 1] == "true"
    assert command[command.index("--enable-thinking") + 1] == "false"
    attachment_index = command.index("--attachments")
    assert command[attachment_index + 1 : attachment_index + 5] == [
        "Type=image",
        "MimeType=image/png",
        "Name=diagram.png",
        "OssObjectKey=user/workspace/diagram.png",
    ]
    assert command[command.index("--query") + 1] == "创建 VPC"
    assert command[command.index("--user-agent") + 1] == bridge.USER_AGENT
    assert "--version" not in command
    assert not any("access-key" in value.lower() for value in command)


def test_build_command_rejects_non_aliyun_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    with pytest.raises(bridge.BridgeError, match="aliyuncs.com"):
        bridge.build_command(_chat_args(endpoint="https://attacker.example"), "hello", None, [])


@pytest.mark.parametrize(
    "endpoint",
    ["evil..aliyuncs.com", "-evil.aliyuncs.com", "恶意.aliyuncs.com", "localhost:0", "127.0.0.1:65536"],
)
def test_build_command_rejects_invalid_endpoint_labels_and_ports(monkeypatch, endpoint: str) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    with pytest.raises(bridge.BridgeError, match="aliyuncs.com"):
        bridge.build_command(_chat_args(endpoint=endpoint), "hello", None, [])


def test_build_command_supports_loopback_endpoint_through_native_cli(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    command = bridge.build_command(_chat_args(endpoint="127.0.0.1:56124"), "hello", None, [])

    assert command[command.index("--endpoint") + 1] == "127.0.0.1:56124"
    assert "--secure" in command
    assert "--skip-secure-verify" in command


def test_build_command_rejects_remote_profile_and_loopback(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/remote/bin/aliyun")
    with pytest.raises(bridge.BridgeError, match="local Profile"):
        bridge.build_command(
            _chat_args(aliyun_cli_execution_mode="remote", profile="local-profile"),
            "hello",
            None,
            [],
        )
    with pytest.raises(bridge.BridgeError, match="public aliyuncs.com"):
        bridge.build_command(
            _chat_args(aliyun_cli_execution_mode="remote", endpoint="127.0.0.1:56124"),
            "hello",
            None,
            [],
        )


def test_build_stop_command_uses_only_published_stop_chat_inputs(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    command = bridge.build_stop_command(
        {
            "aliyunPath": "aliyun",
            "endpoint": "127.0.0.1:56124",
            "connectTimeout": 10,
            "profile": "skill-profile",
            "regionId": "cn-hangzhou",
        },
        "session-1",
    )

    assert command[:3] == ["/usr/local/bin/aliyun", "ros", "stop-chat"]
    assert "--method" not in command
    assert command[command.index("--agent-version") + 1] == "V2"
    assert command[command.index("--session-id") + 1] == "session-1"
    assert command[command.index("--profile") + 1] == "skill-profile"
    assert command[command.index("--region") + 1] == "cn-hangzhou"
    assert command[command.index("--user-agent") + 1] == bridge.USER_AGENT
    assert "--force" not in command
    assert "--secure" in command
    assert "--skip-secure-verify" in command
    assert "--query" not in command
    assert "--biz-mode" not in command
    assert not any("access-key" in value.lower() for value in command)


def test_optional_skill_config_defaults_and_applies_endpoint_and_mode_policy(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    assert bridge.load_skill_config(missing) == {}

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "transport": "aliyun_cli",
                "aliyunCLIExecutionMode": "local",
                "endpoint": "127.0.0.1:56124",
                "allowedAgentModes": ["normal"],
                "managerIdleSeconds": 45,
                "enableThinking": False,
                "aliyunCLIProfile": "fixed-profile",
            }
        ),
        encoding="utf-8",
    )
    config = bridge.load_skill_config(config_path)
    args = argparse.Namespace(command="chat", endpoint=None, mode="normal", profile=None, no_thinking=False)
    bridge.apply_skill_config(args, config)

    assert args.endpoint == "127.0.0.1:56124"
    assert args.transport == "aliyun_cli"
    assert args.aliyun_cli_execution_mode == "local"
    assert config["allowedAgentModes"] == ["normal"]
    assert args.manager_idle_seconds == 45
    assert args.no_thinking is True
    assert args.profile == "fixed-profile"
    assert args.profile_pinned is True

    follow = argparse.Namespace(command="follow")
    bridge.apply_skill_config(follow, config)
    assert follow.manager_idle_seconds == 45

    disallowed = argparse.Namespace(command="chat", endpoint=None, mode="pipeline")
    with pytest.raises(bridge.BridgeError, match="not allowed"):
        bridge.apply_skill_config(disallowed, config)

    conflicting = argparse.Namespace(command="chat", endpoint="ros.aliyuncs.com", mode="normal")
    with pytest.raises(bridge.BridgeError, match="conflicts"):
        bridge.apply_skill_config(conflicting, config)


def test_remote_cli_config_accepts_a_non_secret_environment_allowlist(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "transport": "aliyun_cli",
                "aliyunCLIExecutionMode": "remote",
                "aliyunCLIForwardEnv": ["STAROPS_HIL", "REMOTE_ROUTE"],
            }
        ),
        encoding="utf-8",
    )

    config = bridge.load_skill_config(config_path)
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, config)

    assert args.aliyun_cli_forward_env == ["STAROPS_HIL", "REMOTE_ROUTE"]


def test_remote_cli_environment_allowlist_defaults_to_empty() -> None:
    args = argparse.Namespace(command="check", aliyun_path="aliyun")

    bridge.apply_skill_config(
        args,
        {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote"},
    )

    assert args.aliyun_cli_forward_env == []


def test_transport_cannot_be_overridden_by_a_bridge_command() -> None:
    parser = bridge.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["check", "--transport", "aliyun_cli"])
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--prompt-file", "prompt.txt", "--transport", "aliyun_cli"])


def test_profile_and_thinking_fixed_by_config_reject_conflicting_start_flags() -> None:
    config = {"enableThinking": True, "aliyunCLIProfile": "fixed-profile"}
    wrong_profile = argparse.Namespace(
        command="start",
        endpoint=None,
        mode="normal",
        profile="other-profile",
        no_thinking=False,
    )
    with pytest.raises(bridge.BridgeError) as profile_error:
        bridge.apply_skill_config(wrong_profile, config)
    assert profile_error.value.code == "config_conflict"

    wrong_thinking = argparse.Namespace(
        command="start",
        endpoint=None,
        mode="normal",
        profile="fixed-profile",
        no_thinking=True,
    )
    with pytest.raises(bridge.BridgeError) as thinking_error:
        bridge.apply_skill_config(wrong_thinking, config)
    assert thinking_error.value.code == "config_conflict"


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"unknown": True},
        {"transport": "unsupported"},
        {"transport": True},
        {"aliyunCLIExecutionMode": "unsupported"},
        {"aliyunCLIExecutionMode": "remote"},
        {"aliyunCLIForwardEnv": ["STAROPS_HIL"]},
        {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "local", "aliyunCLIForwardEnv": ["STAROPS_HIL"]},
        {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote", "aliyunCLIForwardEnv": ["BAD-NAME"]},
        {
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
            "aliyunCLIForwardEnv": ["ACCESS_KEY_SECRET"],
        },
        {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote", "aliyunCLIProfile": "profile"},
        {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote", "endpoint": "127.0.0.1:56124"},
        {"endpoint": "https://127.0.0.1:56124"},
        {"endpoint": "attacker.example"},
        {"allowedAgentModes": []},
        {"allowedAgentModes": ["normal", "normal"]},
        {"allowedAgentModes": ["unsupported"]},
        {"managerIdleSeconds": True},
        {"managerIdleSeconds": 0},
        {"managerIdleSeconds": 1.5},
        {"managerIdleSeconds": bridge.MAX_MANAGER_IDLE_SECONDS + 1},
        {"enableThinking": "false"},
        {"enableThinking": 1},
        {"aliyunCLIProfile": None},
        {"aliyunCLIProfile": " padded"},
        {"aliyunCLIProfile": "bad\nprofile"},
    ],
)
def test_skill_config_rejects_invalid_or_unsupported_values(tmp_path: Path, value) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(bridge.BridgeError) as error:
        bridge.load_skill_config(config_path)

    assert error.value.code == "invalid_config"


def test_main_reads_skill_config_before_dispatch(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "transport": "code",
                "endpoint": "localhost:56124",
                "allowedAgentModes": ["pipeline"],
                "managerIdleSeconds": 75,
                "enableThinking": False,
                "aliyunCLIProfile": "fixed-profile",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    def fake_run_start(args):
        captured["endpoint"] = args.endpoint
        captured["transport"] = args.transport
        captured["mode"] = args.mode
        captured["managerIdleSeconds"] = args.manager_idle_seconds
        captured["enableThinking"] = not args.no_thinking
        captured["profile"] = args.profile
        return {"ok": True, "state": "turn-completed"}

    monkeypatch.setattr(bridge, "SKILL_CONFIG_PATH", config_path)
    monkeypatch.setattr(bridge, "run_start_job", fake_run_start)
    exit_code = bridge.main(
        [
            "start",
            "--prompt-file",
            str(tmp_path / "unused.txt"),
            "--mode",
            "pipeline",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "endpoint": "localhost:56124",
        "transport": "code",
        "mode": "pipeline",
        "managerIdleSeconds": 75,
        "enableThinking": False,
        "profile": "fixed-profile",
    }
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_managed_start_persists_default_chain_identity_region_and_thinking(monkeypatch, tmp_path: Path) -> None:
    _clear_code_credential_env(monkeypatch)
    _clear_region_and_profile_env(monkeypatch)
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALIBABACLOUD_REGION_ID", "cn-shenzhen")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("创建 VPC", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge, "ensure_manager", lambda _idle: "manager")
    captured = {}

    def fake_manager_request(record, path, payload, timeout):
        captured.update({"record": record, "path": path, "payload": payload, "timeout": timeout})
        return {"ok": True, "jobId": "job-1", "cursor": 0, "state": "submitted"}

    monkeypatch.setattr(bridge, "_manager_request", fake_manager_request)
    args = bridge.build_parser().parse_args(["start", "--prompt-file", str(prompt)])
    bridge.apply_skill_config(args, {"enableThinking": False})

    result = bridge.run_start_job(args)

    assert result["ok"] is True
    assert captured["payload"]["profile"] is None
    assert captured["payload"]["credentialSource"] is None
    assert captured["payload"]["regionId"] == "cn-shenzhen"
    assert captured["payload"]["noThinking"] is True


def test_check_returns_safe_default_chain_and_effective_skill_policy(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: pytest.fail("the default credential chain must not inspect CLI Profiles"),
    )
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: {"sdk": True})
    monkeypatch.setattr(
        bridge,
        "_code_credentials",
        lambda sdk, aliyun_path, profile, region_id, credential_source: captured.update(
            {
                "sdk": sdk,
                "aliyunPath": aliyun_path,
                "profile": profile,
                "regionId": region_id,
                "credentialSource": credential_source,
            }
        ),
    )
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(
        args,
        {"endpoint": "127.0.0.1:56124", "allowedAgentModes": ["normal"]},
    )

    result = bridge.run_check(args)

    assert result == {
        "ok": True,
        "cli": None,
        "version": None,
        "transport": "code",
        "aliyunCLIExecutionMode": "local",
        "endpoint": "127.0.0.1:56124",
        "allowedAgentModes": ["normal"],
        "managerIdleSeconds": bridge.MANAGER_IDLE_SECONDS,
        "enableThinking": True,
        "debug": False,
        "aliyunCLIProfile": "",
        "currentProfile": {"configured": True, "mode": "DefaultCredentialChain", "regionId": "cn-hangzhou"},
        "startChatReconnectReady": True,
    }
    assert captured == {
        "sdk": {"sdk": True},
        "aliyunPath": "aliyun",
        "profile": None,
        "regionId": "cn-hangzhou",
        "credentialSource": None,
    }


def test_check_rejects_an_unavailable_selected_profile(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: {})
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: (_ for _ in ()).throw(
            bridge.BridgeError("credential_failed", "The selected Alibaba Cloud CLI Profile is not configured.")
        ),
    )
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, {"aliyunCLIProfile": "missing-profile"})

    with pytest.raises(bridge.BridgeError) as error:
        bridge.run_check(args)
    assert error.value.code == "credential_failed"


def test_code_check_delegates_identity_resolution_to_default_credential_chain(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_SECURITY_TOKEN", "fake-env-token")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-shanghai")
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: pytest.fail("default chain must not require CLI"))
    captured = []

    class FakeCredentialClient:
        def __init__(self):
            captured.append("constructed-without-config")

        def get_credential(self):
            return SimpleNamespace(
                access_key_id="fake-chain-ak",
                access_key_secret="fake-chain-secret",
                security_token="fake-chain-token",
            )

    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: {"CredentialClient": FakeCredentialClient})
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, {})

    result = bridge.run_check(args)

    assert result["currentProfile"] == {
        "configured": True,
        "mode": "DefaultCredentialChain",
        "regionId": "cn-shanghai",
    }
    assert captured == ["constructed-without-config"]
    assert result["cli"] is None
    assert result["version"] is None
    assert "fake-env" not in json.dumps(result)


def test_bridge_does_not_parse_credential_environment_variables() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in BRIDGE_SOURCE_PATHS)
    for name in (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABACLOUD_ACCESS_KEY_ID",
        "ALICLOUD_ACCESS_KEY_ID",
        "ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIBABACLOUD_ACCESS_KEY_SECRET",
        "ALICLOUD_ACCESS_KEY_SECRET",
        "ACCESS_KEY_SECRET",
    ):
        assert name not in source


def test_code_start_identity_uses_environment_region_without_inspecting_credentials_or_cli(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    _clear_region_and_profile_env(monkeypatch)
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALIBABACLOUD_REGION_ID", "cn-shanghai")
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: pytest.fail("default credentials must not require a CLI Profile"),
    )
    args = SimpleNamespace(
        transport="code",
        profile=None,
        profile_pinned=False,
        region_id=None,
    )

    bridge._resolve_start_identity(args)

    assert args.profile is None
    assert args.credential_source is None
    assert args.region_id == "cn-shanghai"


def test_code_start_identity_defaults_region_to_hangzhou(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    _clear_region_and_profile_env(monkeypatch)
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    args = SimpleNamespace(
        transport="code",
        profile=None,
        profile_pinned=False,
        region_id=None,
    )

    bridge._resolve_start_identity(args)

    assert args.credential_source is None
    assert args.region_id == "cn-hangzhou"


def test_pinned_profile_identity_ignores_environment_credentials_and_uses_environment_region(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    _clear_region_and_profile_env(monkeypatch)
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALIBABACLOUD_REGION_ID", "cn-beijing")
    captured = []

    def fake_profile(profile):
        captured.append(profile)
        return {"name": profile, "mode": "AK", "regionId": "cn-shanghai"}

    monkeypatch.setattr(bridge, "_selected_cli_profile_record", fake_profile)
    args = SimpleNamespace(
        transport="code",
        profile="fixed-profile",
        profile_pinned=True,
        region_id=None,
    )

    bridge._resolve_start_identity(args)

    assert captured == ["fixed-profile"]
    assert args.profile == "fixed-profile"
    assert args.credential_source == "profile"
    assert args.region_id == "cn-beijing"


def test_aliyun_cli_check_does_not_load_optional_sdk_packages(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command[-1] == "version":
            return SimpleNamespace(returncode=0, stdout=b"3.4.11\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda profile: {
            "name": profile or "default",
            "mode": "AK",
            "regionId": "cn-hangzhou",
            "autoPluginInstall": False,
        },
    )
    monkeypatch.setattr(
        bridge,
        "_local_ros_plugin_status",
        lambda: {"installed": True, "ready": True, "reconnectReady": True, "version": "0.9.1"},
    )
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: pytest.fail("CLI transport must not load SDK packages"))
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, {"transport": "aliyun_cli"})

    result = bridge.run_check(args)

    assert result["ok"] is True
    assert result["transport"] == "aliyun_cli"
    assert result["aliyunCLIExecutionMode"] == "local"
    assert result["rosPluginReady"] is True
    assert result["pluginInstallRequired"] is False
    assert result["pluginAutoInstallEnabled"] is False
    assert result["rosPluginVersion"] == "0.9.1"


@pytest.mark.parametrize(
    ("plugin_status", "auto_install", "install_required"),
    [
        ({"installed": False, "ready": False}, False, True),
        ({"installed": False, "ready": False}, True, True),
        ({"installed": True, "ready": False, "version": "0.7.1"}, True, True),
        ({"installed": True, "ready": True, "reconnectReady": False}, False, True),
    ],
)
def test_local_cli_check_reports_when_skill_must_install_ros_plugin(
    monkeypatch, plugin_status, auto_install: bool, install_required: bool
) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: {
            "name": "default",
            "mode": "AK",
            "regionId": "cn-hangzhou",
            "autoPluginInstall": auto_install,
        },
    )
    monkeypatch.setattr(bridge, "_local_ros_plugin_status", lambda: plugin_status)
    monkeypatch.setattr(
        bridge,
        "_run_check_command",
        lambda _command, required: SimpleNamespace(returncode=0, stdout=b"3.4.11\n", stderr=b""),
    )
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, {"transport": "aliyun_cli"})

    result = bridge.run_check(args)

    assert result["rosPluginReady"] is plugin_status["ready"]
    assert result["pluginAutoInstallEnabled"] is auto_install
    assert result["pluginInstallRequired"] is install_required


def test_remote_aliyun_cli_check_does_not_run_cli_or_read_local_configuration(monkeypatch) -> None:
    monkeypatch.setenv("STAROPS_HIL", "http://route.example/hil")
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/remote/bin/aliyun")
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: pytest.fail("remote CLI mode must not read a local Profile"),
    )
    monkeypatch.setattr(
        bridge,
        "_local_ros_plugin_status",
        lambda: pytest.fail("remote CLI mode must not inspect local plugins"),
    )
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("remote CLI check must not invoke a CLI command"),
    )
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(
        args,
        {
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
            "aliyunCLIForwardEnv": ["STAROPS_HIL"],
            "endpoint": "ros-pre.aliyuncs.com",
        },
    )

    result = bridge.run_check(args)

    assert result["ok"] is True
    assert result["cli"] == "aliyun"
    assert result["version"] is None
    assert result["aliyunCLIExecutionMode"] == "remote"
    assert result["currentProfile"] == {"configured": True, "mode": "RemoteSandbox"}
    assert result["aliyunCLIForwardEnv"] == ["STAROPS_HIL"]
    assert result["aliyunCLIForwardEnvPresent"] == ["STAROPS_HIL"]
    assert "rosPluginReady" not in result
    assert "pluginInstallRequired" not in result


def test_remote_aliyun_cli_identity_does_not_read_local_credentials_or_region(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: pytest.fail("remote CLI mode must not read a local Profile"),
    )
    monkeypatch.setattr(
        bridge,
        "_environment_region",
        lambda: pytest.fail("remote CLI mode must not read a local region"),
    )
    args = SimpleNamespace(
        transport="aliyun_cli",
        aliyun_cli_execution_mode="remote",
        profile=None,
        profile_pinned=False,
        region_id=None,
    )

    bridge._resolve_start_identity(args)

    assert args.profile is None
    assert args.credential_source == "remote"
    assert args.region_id is None


def test_local_ros_plugin_status_requires_binary_and_start_stop_commands(monkeypatch, tmp_path: Path) -> None:
    plugin_root = tmp_path / "aliyun-cli-ros"
    plugin_root.mkdir()
    executable = plugin_root / ("aliyun-cli-ros.exe" if os.name == "nt" else "aliyun-cli-ros")
    executable.write_bytes(b"plugin")
    if os.name != "nt":
        executable.chmod(0o700)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "plugins": {
                    "aliyun-cli-ros": {
                        "version": "0.7.2",
                        "path": str(plugin_root),
                        "cmdNames": ["start-chat", "stop-chat"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ALIBABA_CLOUD_CLI_PLUGINS_DIR", str(tmp_path))

    assert bridge._local_ros_plugin_status() == {
        "installed": True,
        "ready": True,
        "reconnectReady": False,
        "version": "0.7.2",
    }

    manifest.write_text(
        json.dumps(
            {
                "plugins": {
                    "aliyun-cli-ros": {
                        "version": "0.7.1",
                        "path": str(plugin_root),
                        "cmdNames": ["describe-regions"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert bridge._local_ros_plugin_status() == {
        "installed": True,
        "ready": False,
        "reconnectReady": False,
        "version": "0.7.1",
    }

    manifest.write_text(
        json.dumps(
            {
                "plugins": {
                    "aliyun-cli-ros": {
                        "version": "0.9.1",
                        "path": str(plugin_root),
                        "cmdNames": ["start-chat", "stop-chat"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert bridge._local_ros_plugin_status()["reconnectReady"] is True


def test_cli_transport_rejects_client_context_and_remote_profile() -> None:
    local = argparse.Namespace(
        command="start",
        endpoint=None,
        mode="normal",
        profile=None,
        no_thinking=False,
        client_context_file="context.json",
    )
    with pytest.raises(bridge.BridgeError) as context_error:
        bridge.apply_skill_config(local, {"transport": "aliyun_cli"})
    assert context_error.value.code == "unsupported_input"

    remote = argparse.Namespace(
        command="start",
        endpoint=None,
        mode="normal",
        profile="local-profile",
        no_thinking=False,
        client_context_file=None,
    )
    with pytest.raises(bridge.BridgeError) as profile_error:
        bridge.apply_skill_config(
            remote,
            {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote"},
        )
    assert profile_error.value.code == "config_conflict"


def test_workspace_json_inputs_validate_context_and_flatten_attachments(tmp_path: Path) -> None:
    context = tmp_path / "context.json"
    context.write_text('{"preferredLanguage": "zh"}', encoding="utf-8")
    attachments = tmp_path / "attachments.json"
    attachments.write_text(
        json.dumps(
            [
                {
                    "type": "image",
                    "mime_type": "image/webp",
                    "name": "map.webp",
                    "oss_object_key": "user/workspace/map.webp",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert bridge.load_client_context(tmp_path, str(context)) == '{"preferredLanguage":"zh"}'
    assert bridge.load_attachments(tmp_path, str(attachments)) == [
        {
            "Type": "image",
            "MimeType": "image/webp",
            "Name": "map.webp",
            "OssObjectKey": "user/workspace/map.webp",
        }
    ]


def test_client_context_rejects_sensitive_keys_at_any_depth(tmp_path: Path) -> None:
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "region": "cn-hangzhou",
                "page": {"metadata": [{"AccessKeyId": "must-not-be-sent"}]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(bridge.BridgeError) as error:
        bridge.load_client_context(tmp_path, str(context))

    assert error.value.code == "invalid_input"
    assert "credential or secret fields" in error.value.message


def test_permission_query_projects_only_correlated_control_fields(tmp_path: Path) -> None:
    permission_file = tmp_path / "permission.json"
    permission_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": "task-1",
                "contextId": "session-1",
                "inputId": "permission-1",
                "toolUseId": "tool-1",
                "toolName": "bash",
                "safeSummary": "pwd",
                "permissionClass": "pipeline",
            }
        ),
        encoding="utf-8",
    )

    query, response = bridge.load_permission_query(
        tmp_path,
        str(permission_file),
        "allow_once",
        "session-1",
        "pipeline",
    )

    assert query.startswith(bridge.PERMISSION_QUERY_PREFIX + " ")
    assert json.loads(query[len(bridge.PERMISSION_QUERY_PREFIX) :]) == {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    assert response == {
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }


def test_permission_input_preserves_safe_operation_details_and_redacts_nested_secrets() -> None:
    projected = bridge._safe_input(
        {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": "task-1",
            "contextId": "session-1",
            "inputId": "permission-1",
            "toolUseId": "tool-1",
            "toolName": "aliyun_api",
            "scope": "candidate",
            "subPipelineId": "candidate-a",
            "operation": {
                "product": "vpc",
                "action": "CreateVpc",
                "region": "cn-hangzhou",
                "apiCalls": [{"product": "VPC", "action": "CreateVpc", "effect": "change"}],
            },
            "displayParameters": {
                "format": "json",
                "value": {
                    "CidrBlock": "10.0.0.0/16",
                    "Password": "must-not-leak",
                    "Tags": [{"Key": "team", "Value": "platform"}],
                },
            },
            "required": True,
        }
    )

    assert projected is not None
    assert projected["scope"] == "candidate"
    assert projected["subPipelineId"] == "candidate-a"
    assert projected["operation"] == {
        "product": "vpc",
        "action": "CreateVpc",
        "region": "cn-hangzhou",
        "apiCalls": [{"product": "VPC", "action": "CreateVpc", "effect": "change"}],
    }
    assert projected["displayParameters"] == {
        "format": "json",
        "value": {
            "CidrBlock": "10.0.0.0/16",
            "Password": {"redacted": True},
            "Tags": [{"Key": "team", "Value": "platform"}],
        },
    }


def test_permission_query_rejects_session_or_mode_mismatch(tmp_path: Path) -> None:
    permission_file = tmp_path / "permission.json"
    permission_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": "task-1",
                "contextId": "session-1",
                "inputId": "permission-1",
                "toolUseId": "tool-1",
                "permissionClass": "sub_pipeline",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(bridge.BridgeError, match="contextId"):
        bridge.load_permission_query(tmp_path, str(permission_file), "deny", "session-other", "pipeline")
    with pytest.raises(bridge.BridgeError, match="permissionClass"):
        bridge.load_permission_query(tmp_path, str(permission_file), "deny", "session-1", "normal")


def test_prompt_file_must_be_utf8_nonempty_and_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello", encoding="utf-8")
    with pytest.raises(bridge.BridgeError, match="inside"):
        bridge.read_prompt(workspace, str(outside))

    empty = workspace / "empty.txt"
    empty.write_text("  ", encoding="utf-8")
    with pytest.raises(bridge.BridgeError, match="empty"):
        bridge.read_prompt(workspace, str(empty))


def test_authenticated_manager_accepts_existing_workspace_inside_user_owned_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "agenthub-workspace"
    workspace.mkdir()

    assert bridge._trusted_manager_workspace(str(workspace)) == workspace.resolve()

    with pytest.raises(bridge.BridgeError, match="existing directory"):
        bridge._trusted_manager_workspace(str(workspace / "missing"))


def test_authenticated_manager_rejects_workspace_outside_user_owned_roots() -> None:
    with pytest.raises(bridge.BridgeError, match="home or temporary directory"):
        bridge._trusted_manager_workspace(os.path.abspath(os.sep))


def test_sse_parser_handles_heartbeats_multiline_data_and_raw_json() -> None:
    lines = [
        ": comment\n",
        'data: {"object":"heartbeat"}\n',
        "\n",
        "id: v1.stream.with.dots.1\n",
        'data: {"value":\n',
        "data: 1}\n",
        "\n",
        '{"result":{"ok":true}}\n',
    ]
    events = list(bridge.iter_sse_payloads(lines))
    assert [event["payload"] for event in events] == [
        {"object": "heartbeat"},
        {"value": 1},
        {"result": {"ok": True}},
    ]
    assert [event["id"] for event in events] == [None, "v1.stream.with.dots.1", None]


def test_cli_plugin_parser_streams_and_unwraps_each_json_line() -> None:
    first = {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}}
    second = {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}}

    events = list(
        bridge.iter_cli_plugin_payloads(
            [
                json.dumps({"id": "v1.stream.1", "data": first}) + "\n",
                json.dumps({"id": "v1.stream.2", "data": second}) + "\n",
            ]
        )
    )

    assert [(event["id"], event["payload"]) for event in events] == [
        ("v1.stream.1", first),
        ("v1.stream.2", second),
    ]


def test_cli_plugin_parser_accepts_pretty_printed_objects_and_arrays() -> None:
    first = {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}}
    second = {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}}
    pretty_object = json.dumps({"id": "v1.stream.1", "data": first}, indent=2).splitlines(keepends=True)
    pretty_array = json.dumps(
        [{"id": "v1.stream.1", "data": first}, {"id": "v1.stream.2", "data": second}], indent=2
    ).splitlines(keepends=True)

    object_events = list(bridge.iter_cli_plugin_payloads(pretty_object))
    array_events = list(bridge.iter_cli_plugin_payloads(pretty_array))

    assert [event["payload"] for event in object_events] == [first]
    assert [event["payload"] for event in array_events] == [first, second]


def test_cli_plugin_parser_reports_an_unterminated_buffer_as_malformed() -> None:
    events = list(bridge.iter_cli_plugin_payloads(["{\n", '  "data": {\n']))

    assert events == [{"id": None, "payload": None, "raw": '{\n  "data": {'}]


def test_sse_parser_rejects_an_unterminated_event_as_soon_as_its_cumulative_limit_is_exceeded(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bridge, "MAX_SSE_EVENT_BYTES", 30)

    with pytest.raises(bridge.BridgeError, match="event exceeded"):
        list(bridge.iter_sse_payloads(["data: 1234567890\n", "data: 1234567890\n"]))


def test_sse_line_and_event_limits_allow_realistic_large_start_chat_payloads() -> None:
    assert bridge.MAX_SSE_LINE_BYTES == 16 * 1024 * 1024
    assert bridge.MAX_SSE_EVENT_BYTES == 16 * 1024 * 1024


def test_permission_response_acknowledgement_requires_the_full_response_identity() -> None:
    response = {"inputId": "permission-1", "toolUseId": "tool-1", "decision": "allow_once"}
    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
        "accepted": True,
    }

    assert bridge._permission_response_is_acknowledged(response, acknowledgement)
    for field, value in (("inputId", "permission-2"), ("toolUseId", "tool-2"), ("decision", "deny")):
        mismatched = dict(acknowledgement)
        mismatched[field] = value
        assert not bridge._permission_response_is_acknowledged(response, mismatched)
    missing_schema = dict(acknowledgement)
    missing_schema.pop("schemaVersion")
    assert not bridge._permission_response_is_acknowledged(response, missing_schema)


def test_stream_summary_projects_completed_turn_identity_and_artifacts() -> None:
    summary = bridge.StreamSummary()
    summary.apply({"object": "heartbeat"})
    summary.apply(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            text="template ready",
            metadata={"assistantFinal": {"complete": True}},
        )
    )
    summary.apply(
        {
            "result": {
                "artifactUpdate": {
                    "taskId": "task-1",
                    "contextId": "session-1",
                    "artifact": {
                        "artifactId": "artifact-1",
                        "name": "template.yaml",
                        "parts": [{"url": "file:///workspace/template.yaml"}],
                        "metadata": {"mediaType": "application/yaml", "byteSize": 100},
                    },
                }
            }
        }
    )

    result = summary.to_result(0, "")
    assert result["ok"] is True
    assert result["state"] == "turn-completed"
    assert result["presentationRequired"] is True
    assert result["wireState"] == "TASK_STATE_INPUT_REQUIRED"
    assert result["sessionId"] == "session-1"
    assert result["taskId"] == "task-1"
    assert result["iacCodeSessionId"] == "iac-session-1"
    assert result["finalText"] == "template ready"
    assert result["finalTextComplete"] is True
    assert result["heartbeatCount"] == 1
    assert result["artifacts"][0]["name"] == "template.yaml"


def test_stream_summary_final_snapshot_replaces_streamed_deltas() -> None:
    summary = bridge.StreamSummary()
    summary.apply(_status_event(state="TASK_STATE_WORKING", text="template "))
    summary.apply(_status_event(state="TASK_STATE_WORKING", text="ready"))
    summary.apply(
        _status_event(
            state="TASK_STATE_WORKING",
            text="template ready",
            metadata={"assistantFinal": {"complete": True}},
        )
    )
    summary.apply(_status_event(state="TASK_STATE_INPUT_REQUIRED"))

    result = summary.to_result(0, "")

    assert result["state"] == "turn-completed"
    assert result["finalText"] == "template ready"
    assert result["finalTextComplete"] is True


def test_stream_summary_projects_input_required_and_pipeline_milestone() -> None:
    envelope = {
        "schemaVersion": 1,
        "kind": "candidate_selection",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "input-1",
        "prompt": "Choose",
        "options": [{"id": "candidate-a", "label": "A", "summary": "small"}],
        "required": True,
    }
    summary = bridge.StreamSummary()
    summary.apply(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            metadata={
                "input": envelope,
                "pipeline": {
                    "eventType": "input_required",
                    "status": "input_required",
                    "step": {"id": "confirm_and_select", "name": "Confirm"},
                    "data": {"message": "Select a candidate"},
                },
            },
        )
    )
    result = summary.to_result(0, "")
    assert result["state"] == "input-required"
    assert result["presentationRequired"] is True
    assert result["inputRequired"]["inputId"] == "input-1"
    assert result["inputRequired"]["options"][0]["id"] == "candidate-a"
    assert result["milestones"][0]["eventType"] == "input_required"


@pytest.mark.parametrize("kind", ["ask_user_question", "candidate_selection", "permission"])
@pytest.mark.parametrize("batched", [False, True])
def test_input_received_receipt_is_not_a_new_input_boundary(kind, batched) -> None:
    receipt = {
        "eventType": "input_received",
        "status": "working",
        "data": {
            "kind": kind,
            "inputId": "answered-input",
            "options": [{"id": "answer", "label": "Answer"}],
            "allowFreeText": True,
        },
    }
    metadata = {"pipelineBatch": {"events": [receipt]}} if batched else {"pipeline": receipt}
    payload = _status_event(state="TASK_STATE_WORKING", metadata=metadata)
    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(payload)
    projection = bridge._project_managed_stream_event(payload, summary, "pipeline", 2, "primary", None)

    assert summary.to_result(0, "")["state"] == "working"
    assert "inputRequired" not in summary.to_result(0, "")
    assert "inputRequired" not in projection
    assert not bridge._serial_input_boundary(summary.input_required)

    next_input = {
        "schemaVersion": 1,
        "kind": "ask_user_question",
        "contextId": "session-1",
        "requestTaskId": "task-1",
        "inputId": "next-input",
        "prompt": "Development or production?",
        "options": [{"id": "dev", "label": "Development"}],
        "required": True,
    }
    # A receipt preceding a genuine next input in one batch must not hide it.
    payload = _status_event(
        state="TASK_STATE_INPUT_REQUIRED",
        metadata={"pipelineBatch": {"events": [receipt, {
            "eventType": "input_required", "status": "input_required", "data": {"input": next_input},
        }]}},
    )
    summary.apply(payload)
    projection = bridge._project_managed_stream_event(payload, summary, "pipeline", 2, "primary", None)
    assert summary.to_result(0, "")["inputRequired"]["inputId"] == "next-input"
    assert projection["inputRequired"]["inputId"] == "next-input"


def test_stream_summary_keeps_all_sub_pipeline_pending_permissions() -> None:
    pending = [
        {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": "task-1",
            "contextId": "session-1",
            "inputId": "permission-{}".format(index),
            "toolUseId": "tool-{}".format(index),
            "toolName": "bash",
            "prompt": "Allow candidate {}?".format(index),
            "options": [
                {"id": "allow_once", "label": "Allow once"},
                {"id": "deny", "label": "Deny"},
            ],
            "required": True,
        }
        for index in range(2)
    ]
    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(
        _status_event(
            state="TASK_STATE_WORKING",
            metadata={"pendingPermissions": pending},
        )
    )

    result = summary.to_result(0, "")

    assert result["state"] == "input-required"
    assert result["inputRequired"]["inputId"] == "permission-0"
    assert result["inputRequired"]["permissionClass"] == "sub_pipeline"
    assert result["inputRequired"]["permissionRef"].startswith("p-")
    assert [item["inputId"] for item in result["pendingPermissions"]] == ["permission-0", "permission-1"]
    assert {item["permissionClass"] for item in result["pendingPermissions"]} == {"sub_pipeline"}
    assert len({item["permissionRef"] for item in result["pendingPermissions"]}) == 2

    summary.apply(_status_event(state="TASK_STATE_WORKING", metadata={"pendingPermissions": []}))
    resolved = summary.to_result(0, "")
    assert resolved["state"] == "working"
    assert "inputRequired" not in resolved
    assert "pendingPermissions" not in resolved


def test_stream_summary_recognizes_sideband_envelope_without_pending_projection() -> None:
    permission = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-sideband",
        "toolUseId": "tool-sideband",
        "toolName": "bash",
    }
    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(
        _status_event(
            state="TASK_STATE_WORKING",
            metadata={
                "input": permission,
                "pipeline": {"eventType": "permission_requested", "status": "working"},
            },
        )
    )
    result = summary.to_result(0, "")

    assert result["inputRequired"]["permissionClass"] == "sub_pipeline"
    assert result["pendingPermissions"] == [result["inputRequired"]]

    summary.apply(_status_event(state="TASK_STATE_WORKING", metadata={"pendingPermissions": []}))
    summary.apply(_status_event(state="TASK_STATE_INPUT_REQUIRED", metadata={"input": permission}))
    resolved = summary.to_result(0, "")

    assert resolved["state"] == "input-required"
    assert "inputRequired" not in resolved


@pytest.mark.parametrize(
    ("mode", "permission_class"),
    [("normal", "normal"), ("pipeline", "pipeline")],
)
def test_stream_summary_classifies_serial_permission_by_run_mode(mode: str, permission_class: str) -> None:
    permission = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "toolName": "bash",
        "prompt": "Allow?",
        "options": [
            {"id": "allow_once", "label": "Allow once"},
            {"id": "deny", "label": "Deny"},
        ],
        "required": True,
    }
    summary = bridge.StreamSummary(mode=mode)
    summary.apply(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            metadata={"input": permission},
        )
    )

    result = summary.to_result(0, "")

    assert result["state"] == "input-required"
    assert result["inputRequired"]["permissionClass"] == permission_class


def test_stream_summary_projects_sideband_permission_ack() -> None:
    summary = bridge.StreamSummary("session-1", mode="pipeline")
    summary.apply(
        {
            "result": {
                "messageId": "permission-ack-1",
                "taskId": "task-1",
                "contextId": "session-1",
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "mediaType": "application/json",
                        "data": {
                            "schemaVersion": 1,
                            "kind": "permission_ack",
                            "inputId": "permission-1",
                            "toolUseId": "tool-1",
                            "decision": "allow_once",
                            "accepted": True,
                        },
                    }
                ],
            }
        }
    )

    result = summary.to_result(0, "")

    assert result["state"] == "permission-responded"
    assert result["permissionAck"]["accepted"] is True
    assert result["permissionAck"]["inputId"] == "permission-1"


def test_stream_summary_selects_unacknowledged_pending_permission_after_ack() -> None:
    permissions = [
        {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": "task-1",
            "contextId": "session-1",
            "inputId": "permission-{}".format(index),
            "toolUseId": "tool-{}".format(index),
            "toolName": "bash",
            "prompt": "Allow candidate {}?".format(index),
            "options": [{"id": "allow_once", "label": "Allow once"}, {"id": "deny", "label": "Deny"}],
            "required": True,
        }
        for index in range(2)
    ]
    summary = bridge.StreamSummary("session-1", mode="pipeline")
    summary.apply(_status_event(state="TASK_STATE_WORKING", metadata={"pendingPermissions": permissions}))
    summary.apply(
        {
            "result": {
                "messageId": "permission-ack-1",
                "taskId": "task-1",
                "contextId": "session-1",
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "mediaType": "application/json",
                        "data": {
                            "schemaVersion": 1,
                            "kind": "permission_ack",
                            "inputId": "permission-0",
                            "toolUseId": "tool-0",
                            "decision": "allow_once",
                            "accepted": True,
                        },
                    }
                ],
            }
        }
    )

    result = summary.to_result(0, "")

    assert result["state"] == "input-required"
    assert result["inputRequired"]["inputId"] == "permission-1"
    assert [item["inputId"] for item in result["pendingPermissions"]] == ["permission-1"]
    assert result["permissionAck"]["inputId"] == "permission-0"


def test_stream_summary_ignores_permission_echo_after_sideband_ack() -> None:
    summary = bridge.StreamSummary("session-1", mode="pipeline")
    summary.apply(
        {
            "result": {
                "messageId": "permission-ack-1",
                "taskId": "task-1",
                "contextId": "session-1",
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "mediaType": "application/json",
                        "data": {
                            "schemaVersion": 1,
                            "kind": "permission_ack",
                            "inputId": "permission-1",
                            "toolUseId": "tool-1",
                            "decision": "allow_once",
                            "accepted": True,
                        },
                    }
                ],
            }
        }
    )
    summary.apply(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            metadata={
                "input": {
                    "kind": "permission",
                    "inputId": "permission-1",
                    "toolUseId": "tool-1",
                    "toolName": "write_file",
                }
            },
        )
    )

    result = summary.to_result(0, "")

    assert result["state"] == "permission-responded"
    assert result["permissionAck"]["accepted"] is True
    assert "inputRequired" not in result


def test_stream_projection_preserves_bounded_permission_suspend_and_recovery() -> None:
    suspended = bridge._project_stream_event(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            metadata={
                "permissionWait": {"status": "suspended", "resumable": True, "ignored": "secret"},
            },
        ),
        "normal",
        1,
    )
    recovered = bridge._project_stream_event(
        _status_event(
            state="TASK_STATE_WORKING",
            metadata={
                "permissionRecovered": {
                    "inputId": "permission-1",
                    "toolUseId": "tool-1",
                    "ignored": "secret",
                }
            },
        ),
        "normal",
        2,
    )

    assert suspended["type"] == "permission-wait"
    assert suspended["permissionWait"] == {"status": "suspended", "resumable": True}
    assert recovered["type"] == "permission-recovered"
    assert recovered["permissionRecovered"] == {"inputId": "permission-1", "toolUseId": "tool-1"}
    assert "secret" not in json.dumps([suspended, recovered])

    summary = bridge.StreamSummary("session-1", mode="normal")
    summary.apply(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            metadata={"permissionWait": suspended["permissionWait"]},
        )
    )
    result = summary.to_result(0, "")
    assert result["permissionWait"] == {"status": "suspended", "resumable": True}

    summary.apply(
        _status_event(
            state="TASK_STATE_WORKING",
            metadata={"permissionRecovered": recovered["permissionRecovered"]},
        )
    )
    result = summary.to_result(0, "")
    assert "permissionWait" not in result
    assert result["permissionRecovered"] == {"inputId": "permission-1", "toolUseId": "tool-1"}


def test_stream_projection_excludes_internal_pipeline_handoff_context() -> None:
    projection = bridge._project_stream_event(
        _status_event(
            state="TASK_STATE_COMPLETED",
            metadata={
                "pipeline": {
                    "eventType": "pipeline_handoff_ready",
                    "status": "completed",
                    "data": {
                        "action": "switch_to_normal",
                        "targetMode": "normal",
                        "message": "[Pipeline Handoff Context] internal injected context",
                    },
                }
            },
        ),
        "pipeline",
        1,
    )

    assert projection["type"] == "terminal"
    assert projection["normalHandoffReady"] is True
    assert "milestones" not in projection
    assert "Pipeline Handoff Context" not in json.dumps(projection)

    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(
        _status_event(
            state="TASK_STATE_COMPLETED",
            metadata={
                "pipeline": {
                    "eventType": "pipeline_handoff_ready",
                    "status": "completed",
                    "visibility": "committed",
                    "data": {
                        "action": "switch_to_normal",
                        "targetMode": "normal",
                        "message": "[Pipeline Handoff Context] internal injected context",
                    },
                }
            },
        )
    )
    result = summary.to_result(0, "")
    assert result["state"] == "completed"
    assert result["normalHandoffReady"] is True
    assert result["conversationMode"] == "normal"
    assert "Pipeline Handoff Context" not in json.dumps(result)


def test_pipeline_stream_summary_does_not_regress_terminal_handoff_on_trailing_working() -> None:
    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(
        _status_event(
            state="TASK_STATE_COMPLETED",
            metadata={
                "pipelineBatch": {
                    "events": [
                        {
                            "eventType": "step_completed",
                            "status": "completed",
                            "step": {"id": "deploying", "name": "Deploy"},
                            "data": {
                                "conclusionField": "deployment",
                                "conclusion": {
                                    "status": "success",
                                    "stack_id": "stack-1",
                                    "resources_created": ["vsw-1"],
                                },
                            },
                        },
                        {
                            "eventType": "pipeline_handoff_ready",
                            "status": "completed",
                            "data": {"action": "switch_to_normal", "targetMode": "normal"},
                        },
                    ]
                }
            },
        )
    )
    summary.apply(
        _status_event(
            state="TASK_STATE_WORKING",
            text="final deployment summary",
            metadata={"assistantFinal": {"complete": True}},
        )
    )

    result = summary.to_result(0, "")

    assert result["state"] == "completed"
    assert result["wireState"] == "TASK_STATE_COMPLETED"
    assert result["normalHandoffReady"] is True
    assert result["conversationMode"] == "normal"
    assert result["pipelineResult"] == {
        "status": "success",
        "stack_id": "stack-1",
        "resources_created": ["vsw-1"],
    }


@pytest.mark.parametrize(
    "stale_metadata",
    [
        {
            "input": {
                "schemaVersion": 1,
                "kind": "candidate_selection",
                "requestTaskId": "task-1",
                "contextId": "session-1",
                "inputId": "stale-candidate",
                "prompt": "Choose",
                "options": [{"id": "candidate-a", "label": "A"}],
                "required": True,
            }
        },
        {
            "pendingPermissions": [
                {
                    "schemaVersion": 1,
                    "kind": "permission",
                    "requestTaskId": "task-1",
                    "contextId": "session-1",
                    "inputId": "stale-permission",
                    "toolUseId": "tool-1",
                    "toolName": "aliyun_api",
                    "prompt": "Allow?",
                    "required": True,
                }
            ]
        },
    ],
)
def test_stream_summary_terminal_state_rejects_stale_trailing_wait_boundaries(stale_metadata: dict) -> None:
    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(_status_event(state="TASK_STATE_COMPLETED"))
    summary.apply(_status_event(state="TASK_STATE_WORKING", metadata=stale_metadata))

    result = summary.to_result(0, "")

    assert result["state"] == "completed"
    assert result["wireState"] == "TASK_STATE_COMPLETED"
    assert "inputRequired" not in result
    assert "pendingPermissions" not in result
    assert "permissionWait" not in result


def test_managed_primary_terminal_hides_trailing_stale_input_before_finish(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "artifacts": [],
        },
    )
    summary = bridge.StreamSummary(mode="pipeline")
    completed = _status_event(state="TASK_STATE_COMPLETED")
    stale_input = _status_event(
        state="TASK_STATE_WORKING",
        metadata={
            "input": {
                "schemaVersion": 1,
                "kind": "candidate_selection",
                "requestTaskId": "task-1",
                "contextId": "session-1",
                "inputId": "stale-candidate",
                "prompt": "Choose",
                "options": [{"id": "candidate-a", "label": "A"}],
                "required": True,
            }
        },
    )
    for payload in (completed, stale_input):
        summary.apply(payload)
        bridge._append_projection(
            job_id,
            bridge._project_managed_stream_event(payload, summary, "pipeline", 1, "primary", None),
        )

    job = bridge._load_state_json(job_path)
    followed = bridge._follow_job_local(job_id, 0, 0)

    assert job["state"] == "working"
    assert job["primaryStreamTerminalSeen"] is True
    assert "inputRequired" not in job
    assert "pendingPermissions" not in job
    assert followed["state"] == "working"
    assert "inputRequired" not in followed
    assert all("inputRequired" not in event for event in bridge._read_spool(spool))


def test_managed_sideband_terminal_hides_its_stale_input_without_closing_parent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "b" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "activeRequestSeq": 2,
            "workerPid": os.getpid(),
            "sidebandWorkerPid": os.getpid(),
            "sidebandWorkerToken": "sideband-token",
            "artifacts": [],
        },
    )
    summary = bridge.StreamSummary(mode="pipeline")
    completed = _status_event(state="TASK_STATE_COMPLETED")
    stale_input = _status_event(
        state="TASK_STATE_WORKING",
        metadata={
            "pendingPermissions": [
                {
                    "schemaVersion": 1,
                    "kind": "permission",
                    "requestTaskId": "task-1",
                    "contextId": "session-1",
                    "inputId": "stale-permission",
                    "toolUseId": "tool-1",
                    "toolName": "aliyun_api",
                    "prompt": "Allow?",
                    "required": True,
                }
            ]
        },
    )
    for payload in (completed, stale_input):
        summary.apply(payload)
        bridge._append_projection(
            job_id,
            bridge._project_managed_stream_event(
                payload,
                summary,
                "pipeline",
                2,
                "sideband",
                "sideband-token",
            ),
        )

    job = bridge._load_state_json(job_path)
    followed = bridge._follow_job_local(job_id, 0, 0)

    assert job["state"] == "working"
    assert job["workerPid"] == os.getpid()
    assert "primaryStreamTerminalSeen" not in job
    assert "inputRequired" not in job
    assert "pendingPermissions" not in job
    assert followed["state"] == "working"
    assert "inputRequired" not in followed
    assert all("inputRequired" not in event for event in bridge._read_spool(spool))


def test_pipeline_raw_input_boundary_stays_input_required_and_projects_deployment_result() -> None:
    summary = bridge.StreamSummary(mode="pipeline")
    summary.apply(
        _status_event(
            state="TASK_STATE_INPUT_REQUIRED",
            metadata={
                "pipelineBatch": {
                    "events": [
                        {
                            "eventType": "step_completed",
                            "status": "completed",
                            "step": {"id": "deploy"},
                            "data": {
                                "conclusionField": "deployment",
                                "conclusion": {
                                    "status": "success",
                                    "stack_id": "stack-1",
                                    "resources_created": ["vpc-1"],
                                    "outputs": {"VpcId": "vpc-1"},
                                },
                            },
                        }
                    ]
                }
            },
        )
    )
    result = summary.to_result(0, "")
    assert result["state"] == "input-required"
    assert "inputRequired" not in result
    assert result["pipelineResult"] == {
        "status": "success",
        "stack_id": "stack-1",
        "resources_created": ["vpc-1"],
        "outputs": {"VpcId": "vpc-1"},
    }


def test_cli_failure_redacts_secrets_from_error() -> None:
    summary = bridge.StreamSummary("session-1")
    result = summary.to_result(
        1,
        'AccessKeySecret=super-secret Authorization: Bearer bearer-secret {"SecurityToken":"token-secret"}',
    )
    message = result["error"]["message"]
    assert result["state"] == "failed"
    assert "super-secret" not in message
    assert "bearer-secret" not in message
    assert "token-secret" not in message
    assert message.count("[REDACTED]") == 3


def test_stream_summary_preserves_retryable_session_backup_error() -> None:
    summary = bridge.StreamSummary("session-1")
    summary.apply(
        {
            "object": "response",
            "status": "failed",
            "error": {
                "code": -32602,
                "message": "Session backup is still synchronizing. Retry after 3 seconds.",
                "data": {"code": bridge.SESSION_BACKUP_NOT_READY_CODE, "retryable": True},
            },
        }
    )

    result = summary.to_result(0, "")

    assert result["state"] == "failed"
    assert result["error"] == {
        "code": bridge.SESSION_BACKUP_NOT_READY_CODE,
        "message": "Session backup is still synchronizing. Retry after 3 seconds.",
        "retryable": True,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": -32602,
            "message": "Session backup is still synchronizing. Retry after 3 seconds.",
            "data": {"code": "SESSION_BACKUP_NOT_READY", "retryable": True},
        },
        {
            "Code": "SESSION_BACKUP_NOT_READY",
            "Message": "Session backup is still synchronizing. Retry after 3 seconds.",
        },
    ],
)
def test_session_backup_not_ready_error_accepts_sse_and_http_shapes(payload) -> None:
    error = bridge._session_backup_not_ready_error(payload)

    assert error is not None
    assert error.code == bridge.SESSION_BACKUP_NOT_READY_CODE
    assert error.retryable is True


def test_run_chat_consumes_fake_cli_stream_without_network(monkeypatch, tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")
    output = json.dumps(
        {
            "id": "v1.stream.1",
            "data": _status_event(
                state="TASK_STATE_INPUT_REQUIRED",
                text="done",
                metadata={"assistantFinal": {"complete": True}},
            ),
        },
        separators=(",", ":"),
    )
    output += "\n"
    captured = {}

    class FakeProcess:
        def __init__(self):
            self.stdout = io.StringIO(output)

        def wait(self, timeout=None):
            del timeout
            return 0

        def poll(self):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: pytest.fail("CLI transport must not load SDK packages"))
    args = SimpleNamespace(
        cwd=str(tmp_path),
        prompt_file=str(prompt),
        client_context_file=None,
        attachments_file=None,
        session_id=None,
        aliyun_path="aliyun",
        transport="aliyun_cli",
        endpoint="ros.aliyuncs.com",
        connect_timeout=10,
        read_timeout=600,
        profile=None,
        region_id="cn-hangzhou",
        no_thinking=False,
        mode="normal",
    )

    result = bridge.run_chat(args)
    assert result["state"] == "turn-completed"
    assert result["finalText"] == "done"
    assert os.path.normcase(captured["cwd"]) == os.path.normcase(str(tmp_path))
    assert captured["command"][captured["command"].index("--query") + 1] == "hello"


def test_code_transport_streams_sdk_signed_request_without_cli_response_buffering(monkeypatch, tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")
    event = _status_event(
        state="TASK_STATE_COMPLETED",
        text="done",
        metadata={"assistantFinal": {"complete": True}},
    )
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def __init__(self):
            self.closed = False
            self.lines = iter(
                [
                    b"id: v1.stream.1\n",
                    ("data: " + json.dumps(event, separators=(",", ":")) + "\n").encode(),
                    b"\n",
                ]
            )

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.lines)

        def close(self):
            self.closed = True

    response = FakeResponse()

    def fake_open(
        operation, parameters, endpoint, profile, region_id, aliyun_path, connect_timeout, read_timeout, **kwargs
    ):
        captured.update(
            {
                "operation": operation,
                "parameters": parameters,
                "endpoint": endpoint,
                "profile": profile,
                "regionId": region_id,
                "aliyunPath": aliyun_path,
                "connectTimeout": connect_timeout,
                "readTimeout": read_timeout,
                "options": kwargs,
            }
        )
        return response

    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not invoke CLI"))
    monkeypatch.setattr(bridge, "_open_code_request", fake_open)
    args = SimpleNamespace(
        cwd=str(tmp_path),
        prompt_file=str(prompt),
        client_context_file=None,
        attachments_file=None,
        session_id=None,
        aliyun_path="aliyun",
        transport="code",
        endpoint="127.0.0.1:56124",
        connect_timeout=10,
        read_timeout=600,
        profile="skill-profile",
        region_id="cn-hangzhou",
        no_thinking=False,
        mode="normal",
    )

    result = bridge.run_chat(args)

    assert result["state"] == "turn-completed"
    assert result["finalText"] == "done"
    assert captured["operation"] == "StartChat"
    assert captured["parameters"]["Query"] == "hello"
    assert captured["parameters"]["Mode"] == "IaCCodeNormal"
    assert captured["endpoint"] == "127.0.0.1:56124"
    assert captured["profile"] == "skill-profile"
    assert captured["regionId"] == "cn-hangzhou"
    assert captured["aliyunPath"] == "aliyun"
    assert response.closed is True


def test_open_code_request_loads_cli_profile_and_streams_with_sdk_signing(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    captured = {}

    class FakeProvider:
        def __init__(self, profile_name=None):
            captured["profile"] = profile_name

    class FakeCredentialClient:
        def __init__(self, provider=None):
            captured["provider"] = provider

        def get_credential(self):
            return SimpleNamespace(
                access_key_id="fake-ak",
                access_key_secret="fake-secret",
                security_token="fake-token",
            )

    class FakeRaw:
        def read(self, _maximum, decode_content=False):
            captured["decodeContent"] = decode_content
            return b""

    class FakeSdkResponse:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}
        raw = FakeRaw()

        def iter_lines(self, **_kwargs):
            return iter([])

        def close(self):
            captured["responseClosed"] = True

    class FakeSession:
        def request(self, **values):
            captured["http"] = values
            return FakeSdkResponse()

        def close(self):
            captured["sessionClosed"] = True

    sdk = bridge._load_code_sdk()
    sdk["CredentialClient"] = FakeCredentialClient
    sdk["CLIProfileCredentialsProvider"] = FakeProvider
    sdk["requests"] = SimpleNamespace(Session=FakeSession)
    monkeypatch.setattr(sdk["OpenApiUtils"], "get_timestamp", staticmethod(lambda: "2026-08-26T03:00:00Z"))
    monkeypatch.setattr(sdk["OpenApiUtils"], "get_nonce", staticmethod(lambda: "fixed-nonce"))
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: sdk)
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda profile: {"name": profile, "mode": "AK"},
    )

    response = bridge._open_code_request(
        "StartChat",
        {"AgentVersion": "V2", "Query": "hello"},
        "127.0.0.1:56124",
        "skill-profile",
        "cn-hangzhou",
        "aliyun",
        10,
        600,
        "profile",
    )

    assert captured["profile"] == "skill-profile"
    assert captured["http"]["url"] == "https://127.0.0.1:56124/?AgentVersion=V2&Query=hello"
    assert captured["http"]["method"] == "POST"
    assert captured["http"]["stream"] is True
    assert captured["http"]["verify"] is False
    headers = captured["http"]["headers"]
    assert headers["x-acs-action"] == "StartChat"
    assert headers["x-acs-version"] == "2019-09-10"
    assert headers["x-acs-date"] == "2026-08-26T03:00:00Z"
    assert headers["x-acs-signature-nonce"] == "fixed-nonce"
    assert headers["x-acs-security-token"] == "fake-token"
    assert headers["user-agent"] == bridge.USER_AGENT
    assert headers["Authorization"].startswith("ACS3-HMAC-SHA256 Credential=fake-ak,SignedHeaders=")
    assert "SignatureVersion" not in captured["http"]["url"]
    assert "HMAC-SHA1" not in headers["Authorization"]
    response.close()
    assert captured["responseClosed"] is True
    assert captured["sessionClosed"] is True


def test_code_credentials_use_default_sdk_chain_without_profile_inspection(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALICLOUD_SECURITY_TOKEN", "fake-env-token")
    captured = []

    class FakeCredentialClient:
        def __init__(self):
            captured.append("constructed-without-config")

        def get_credential(self):
            return SimpleNamespace(
                access_key_id="fake-chain-ak",
                access_key_secret="fake-chain-secret",
                security_token="fake-chain-token",
            )

    sdk = {
        "CredentialClient": FakeCredentialClient,
        "CLIProfileCredentialsProvider": lambda **_kwargs: pytest.fail("default chain must not pin a profile"),
    }
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda *_args: pytest.fail("default chain must not inspect Profile"),
    )

    credentials = bridge._code_credentials(sdk, "aliyun", "ignored-profile", "cn-hangzhou")

    assert captured == ["constructed-without-config"]
    assert credentials == ("fake-chain-ak", "fake-chain-secret", "fake-chain-token")


def test_code_credentials_with_profile_source_do_not_fall_back_to_environment(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    captured = {}

    class FakeProvider:
        def __init__(self, profile_name=None):
            captured["profile"] = profile_name

    class FakeCredentialClient:
        def __init__(self, provider=None):
            captured["provider"] = provider

        def get_credential(self):
            return SimpleNamespace(
                access_key_id="fake-profile-ak",
                access_key_secret="fake-profile-secret",
                security_token=None,
            )

    sdk = {
        "CredentialClient": FakeCredentialClient,
        "CLIProfileCredentialsProvider": FakeProvider,
    }
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda profile: {"name": profile, "mode": "AK"},
    )

    credentials = bridge._code_credentials(
        sdk,
        "aliyun",
        "fixed-profile",
        "cn-hangzhou",
        "profile",
    )

    assert captured["profile"] == "fixed-profile"
    assert credentials == ("fake-profile-ak", "fake-profile-secret", None)


def test_code_credentials_delegate_oauth_profile_resolution_and_refresh_to_sdk(monkeypatch) -> None:
    captured = {}

    class FakeProvider:
        def __init__(self, profile_name=None):
            captured["profile"] = profile_name

    class FakeCredentialClient:
        def __init__(self, provider=None):
            captured["provider"] = provider

        def get_credential(self):
            return SimpleNamespace(
                access_key_id="fake-sdk-ak",
                access_key_secret="fake-sdk-secret",
                security_token="fake-sdk-token",
            )

    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda profile: {"name": profile, "mode": "OAuth"},
    )
    sdk = {
        "CredentialClient": FakeCredentialClient,
        "CLIProfileCredentialsProvider": FakeProvider,
    }

    credentials = bridge._code_credentials(sdk, "aliyun", "oauth-profile", "cn-hangzhou", "profile")

    assert captured["profile"] == "oauth-profile"
    assert isinstance(captured["provider"], FakeProvider)
    assert credentials == ("fake-sdk-ak", "fake-sdk-secret", "fake-sdk-token")


def test_code_transport_uses_same_profile_and_endpoint_for_stop_chat(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class FakeResponse:
        def read(self, _maximum):
            return b'{"Status":"Stopped","SessionId":"session-1","RequestId":"request-1"}'

        def close(self):
            captured["closed"] = True

    def fake_open(
        operation, parameters, endpoint, profile, region_id, aliyun_path, connect_timeout, read_timeout, **kwargs
    ):
        captured.update(
            {
                "operation": operation,
                "parameters": parameters,
                "endpoint": endpoint,
                "profile": profile,
                "regionId": region_id,
                "aliyunPath": aliyun_path,
                "connectTimeout": connect_timeout,
                "readTimeout": read_timeout,
                "options": kwargs,
            }
        )
        return FakeResponse()

    monkeypatch.setattr(bridge, "_open_code_request", fake_open)
    result = bridge._run_stop_chat(
        {
            "workspace": str(tmp_path),
            "transport": "code",
            "endpoint": "127.0.0.1:56124",
            "profile": "skill-profile",
            "regionId": "cn-hangzhou",
            "aliyunPath": "aliyun",
        },
        "session-1",
    )

    assert result == {"status": "Stopped", "sessionId": "session-1", "requestId": "request-1"}
    assert captured["operation"] == "StopChat"
    assert captured["parameters"] == {"AgentVersion": "V2", "SessionId": "session-1"}
    assert captured["endpoint"] == "127.0.0.1:56124"
    assert captured["profile"] == "skill-profile"
    assert captured["regionId"] == "cn-hangzhou"
    assert captured["aliyunPath"] == "aliyun"
    assert captured["options"] == {"credential_source": None, "error_code": "stop_chat_failed"}
    assert captured["closed"] is True


@pytest.mark.parametrize("execution_mode,expected_timeout", [("local", 60.0), ("remote", 1800.0)])
def test_stop_chat_allows_remote_foreground_approval_wait(monkeypatch, execution_mode, expected_timeout) -> None:
    captured = {}

    def fake_run(_command, **kwargs):
        captured.update(kwargs)
        # Simulate an executor that needs more than two minutes for approval,
        # without a real wait or a real cloud call.
        if execution_mode == "remote" and kwargs["timeout"] <= 120:
            raise subprocess.TimeoutExpired("aliyun", kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=b'{"Status":"Stopped","SessionId":"s-1"}', stderr=b"")

    monkeypatch.setattr(bridge, "build_stop_command", lambda _job, _session: ["aliyun", "ros", "stop-chat"])
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    job = {"transport": "aliyun_cli", "aliyunCLIExecutionMode": execution_mode}
    assert bridge._run_stop_chat(job, "s-1") == {"status": "Stopped", "sessionId": "s-1"}
    assert captured["timeout"] == expected_timeout


@pytest.mark.parametrize("command", ["cancel", "follow"])
@pytest.mark.parametrize("execution_mode,expected_timeout", [("local", 75.0), ("remote", 1815.0)])
def test_cancel_manager_wait_matches_remote_executor_approval_budget(
    monkeypatch, tmp_path: Path, command, execution_mode, expected_timeout
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    job = {
        "transport": "aliyun_cli", "aliyunCLIExecutionMode": execution_mode,
        "stopRequestedAt": 1, "aliyunCLIForwardEnv": [],
    }
    bridge._atomic_json(bridge._job_paths(job_id)[1], job)
    monkeypatch.setattr(bridge, "ensure_manager", lambda _idle: {})
    requests = []

    def fake_request(_record, path, _payload, timeout):
        requests.append((path, timeout))
        return {"ok": True, "state": "canceled"}

    monkeypatch.setattr(bridge, "_manager_request", fake_request)
    arguments = SimpleNamespace(job_id=job_id, manager_idle_seconds=60, aliyun_cli_forward_env=[],
                                wait_seconds=0, cursor=0)
    if command == "cancel":
        bridge.run_cancel_job(arguments)
    else:
        bridge.run_follow_job(arguments)
        if execution_mode == "local":
            assert requests == [("/follow", 15.0)]
            return
    assert requests[0] == ("/cancel", expected_timeout)


def test_run_respond_sends_json_as_the_only_start_chat_control_payload(monkeypatch, tmp_path: Path) -> None:
    permission_file = tmp_path / "permission.json"
    permission_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": "task-1",
                "contextId": "session-1",
                "inputId": "permission-1",
                "toolUseId": "tool-1",
                "permissionClass": "sub_pipeline",
            }
        ),
        encoding="utf-8",
    )
    acknowledgement = {
        "result": {
            "messageId": "permission-ack-1",
            "taskId": "task-1",
            "contextId": "session-1",
            "role": "ROLE_AGENT",
            "parts": [
                {
                    "mediaType": "application/json",
                    "data": {
                        "schemaVersion": 1,
                        "kind": "permission_ack",
                        "inputId": "permission-1",
                        "toolUseId": "tool-1",
                        "decision": "deny",
                        "accepted": True,
                    },
                }
            ],
        }
    }
    completed = _status_event(state="TASK_STATE_COMPLETED")
    output = json.dumps(
        [
            {"id": "v1.response.1", "data": acknowledgement},
            {"id": "v1.response.2", "data": completed},
        ],
        separators=(",", ":"),
    )
    output += "\n"
    captured = {}

    class FakeProcess:
        def __init__(self):
            self.stdout = io.StringIO(output)

        def wait(self, timeout=None):
            del timeout
            return 0

        def poll(self):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    args = SimpleNamespace(
        cwd=str(tmp_path),
        input_file=str(permission_file),
        decision="deny",
        session_id="session-1",
        aliyun_path="aliyun",
        endpoint="ros.aliyuncs.com",
        connect_timeout=10,
        read_timeout=600,
        profile=None,
        region_id="cn-hangzhou",
        no_thinking=True,
        mode="pipeline",
    )

    result = bridge.run_respond(args)
    command = captured["command"]
    query_text = command[command.index("--query") + 1]
    assert query_text.startswith(bridge.PERMISSION_QUERY_PREFIX + " ")
    query = json.loads(query_text[len(bridge.PERMISSION_QUERY_PREFIX) :])

    assert result["state"] == "completed"
    assert result["permissionResponse"]["decision"] == "deny"
    assert query["decision"] == "deny"
    assert command[command.index("--enable-thinking") + 1] == "false"
    assert "--client-context" not in command
    assert "--attachments" not in command


def _wait_for_pid_exit(pid: int, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and bridge._pid_alive(pid):
        time.sleep(0.05)


@pytest.mark.parametrize('client_delay', [0.0, 0.7])
def test_manager_is_loopback_authenticated_reused_and_recovers_after_idle_shutdown(
    monkeypatch, tmp_path: Path, client_delay: float,
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    # Validate reuse before enabling rapid idle expiry. A loaded Windows caller
    # can take longer than 0.3s between requests; recycling then is correct.
    # The unchanged 0.1s reconfiguration below still tests real idle shutdown.
    monkeypatch.setattr(bridge, "MANAGER_IDLE_SECONDS", 60)

    first = bridge.ensure_manager()
    time.sleep(client_delay)
    second = bridge.ensure_manager()

    assert second == first
    assert first["port"] > 0
    assert len(first["token"]) >= 32
    assert len(first["generation"]) == 32
    assert bridge._pid_alive(first["pid"])
    if os.name != "nt":
        assert bridge._manager_record_path().stat().st_mode & 0o777 == 0o600

    reconfigured = bridge.ensure_manager(0.1)
    assert reconfigured["pid"] == first["pid"]
    assert reconfigured["generation"] == first["generation"]
    assert reconfigured["idleSeconds"] == 0.1

    _wait_for_pid_exit(first["pid"])
    assert not bridge._pid_alive(first["pid"])

    third = bridge.ensure_manager(0.1)
    assert third["generation"] != first["generation"]
    assert third["token"] != first["token"]
    assert third["pid"] != first["pid"]
    _wait_for_pid_exit(third["pid"])


def test_manager_waits_for_first_authorized_health_before_idle_shutdown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    original_manager_matches = bridge._manager_matches
    delayed_health_check = False

    def delay_first_authorized_health_check(record):
        nonlocal delayed_health_check
        if delayed_health_check:
            return original_manager_matches(record)
        invalid_record = dict(record, token="invalid")
        deadline = time.monotonic() + bridge.MANAGER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                bridge._manager_request(invalid_record, "/health", timeout=0.2)
            except bridge.BridgeError as exc:
                if exc.code == "unauthorized":
                    delayed_health_check = True
                    time.sleep(0.7)
                    break
            time.sleep(0.05)
        return original_manager_matches(record)

    monkeypatch.setattr(bridge, "_manager_matches", delay_first_authorized_health_check)

    manager = bridge.ensure_manager(0.1)

    assert delayed_health_check
    _wait_for_pid_exit(manager["pid"])


def test_manager_does_not_idle_during_stop_chat_approval_without_worker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    entered = tmp_path / "stop-entered"
    release = tmp_path / "stop-release"
    cli = _write_fake_aliyun(tmp_path, """import json, time
from pathlib import Path
Path(%r).touch()
release = Path(%r)
deadline = time.monotonic() + 15
while not release.exists():
    assert time.monotonic() < deadline
    time.sleep(0.02)
print(json.dumps({'Status': 'Stopped', 'SessionId': 'session-1'}))
""" % (str(entered), str(release)))
    job_id = "a" * 32
    bridge._atomic_json(bridge._job_paths(job_id)[1], {
        "transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote",
        "sessionId": "session-1", "state": "working", "mode": "normal", "turn": 1,
        "workspace": str(tmp_path), "endpoint": "ros.aliyuncs.com", "aliyunPath": str(cli),
    })
    manager = bridge.ensure_manager(1)
    outcome = []

    def cancel():
        try:
            outcome.append(bridge._manager_request(manager, "/cancel", {"jobId": job_id}, timeout=15))
        except bridge.BridgeError as exc:
            outcome.append(exc.code)

    caller = threading.Thread(target=cancel)
    caller.start()
    try:
        deadline = time.monotonic() + 10
        while not entered.exists():
            assert time.monotonic() < deadline, "StopChat did not reach approval wait"
            time.sleep(0.02)
        time.sleep(1.6)
        assert bridge._pid_alive(manager["pid"]), "An in-flight StopChat is not idle even without a worker"
        release.touch()
        caller.join(5)
        assert not caller.is_alive()
        assert outcome[0]["stopStatus"] == "Stopped"
        assert outcome[0]["state"] == "canceled"
    finally:
        release.touch()
        caller.join(5)
        _wait_for_pid_exit(manager["pid"])
    assert not bridge._pid_alive(manager["pid"]), "Idle shutdown should resume after the request finishes"


def test_manager_idle_countdown_starts_after_sse_worker_exits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # This stdlib-only process test needs Popen.pid to identify the interpreter
    # itself. Windows venv python.exe is a redirector with a different PID from
    # the worker it launches; use the base interpreter for the whole process tree.
    python_executable = getattr(sys, "_base_executable", None) or sys.executable
    monkeypatch.setattr(bridge, "sys", SimpleNamespace(executable=python_executable))
    monkeypatch.delenv("__PYVENV_LAUNCHER__", raising=False)
    fake_cli = Path(python_executable)
    worker_pid_path = workspace / "worker-pid"
    release_worker = workspace / "release-worker"
    (workspace / "ros").write_text(
        "import json, os, time\n"
        + "from pathlib import Path\n"
        + "pid_path = Path({!r})\n".format(str(worker_pid_path))
        + "pid_path.with_suffix('.tmp').write_text(str(os.getppid()), encoding='utf-8')\n"
        + "pid_path.with_suffix('.tmp').replace(pid_path)\n"
        + "release = Path({!r})\n".format(str(release_worker))
        + "deadline = time.monotonic() + 25\n"
        + "while not release.exists():\n"
        + "    assert time.monotonic() < deadline, 'test did not release worker'\n"
        + "    time.sleep(0.05)\n"
        + "event = {'result': {'statusUpdate': {'taskId': 'task-1', 'contextId': 'session-1', "
        + "'status': {'state': 'TASK_STATE_INPUT_REQUIRED', 'message': {'role': 'ROLE_AGENT', "
        + "'parts': [{'text': 'done'}]}}, 'metadata': {'iac_code': {'assistantFinal': "
        + "{'complete': True}}, 'iacCodeSessionId': 'iac-1'}}}}\n"
        + "print(json.dumps({'id': 'v1.stream.1', 'data': event}), flush=True)\n",
        encoding="utf-8",
    )

    # Leave enough scheduling headroom for a loaded Windows xdist runner; this
    # test is about when the idle countdown starts, not sub-second timing.
    idle_seconds = 5.0
    manager = bridge.ensure_manager(idle_seconds)
    try:
        started = bridge._manager_request(
            manager,
            "/start",
            {
                "workspace": str(workspace),
                "prompt": "explain VPC",
                "mode": "normal",
                "transport": "aliyun_cli",
                "endpoint": "ros.aliyuncs.com",
                "regionId": "cn-hangzhou",
                "aliyunPath": str(fake_cli),
            },
        )
        # /start returns only after workerPid has been committed. A fixed fake-CLI
        # sleep can finish before that commit on Windows and leave a stale PID.
        # Hold the registered worker beyond the idle threshold to test the actual
        # contract: active work prevents manager idle shutdown.
        root, job_path, _spool = bridge._job_paths(started["jobId"])
        with bridge.StateLock(root / ".job.lock"):
            assert bridge._load_state_json(job_path)["workerPid"] == started["workerPid"]
        deadline = time.monotonic() + 10
        while not worker_pid_path.exists():
            assert time.monotonic() < deadline, "Fake CLI did not start"
            time.sleep(0.05)
        assert int(worker_pid_path.read_text(encoding="utf-8")) == started["workerPid"]
        time.sleep(idle_seconds + 0.5)
        assert bridge._pid_alive(started["workerPid"])
        assert bridge._pid_alive(manager["pid"])
        release_worker.touch()

        deadline = time.monotonic() + 10
        while True:
            # Use the worker's cross-process lock: an unlocked read can race
            # atomic replacement of job.json and fail with PermissionError on Windows.
            with bridge.StateLock(root / ".job.lock"):
                job = bridge._load_state_json(job_path)
            if not isinstance(job.get("workerPid"), int):
                break
            assert time.monotonic() < deadline, {
                "state": job.get("state"),
                "workerPid": job.get("workerPid"),
                "workerStartedAt": job.get("workerStartedAt"),
                "workerExitedAt": job.get("workerExitedAt"),
            }
            time.sleep(0.02)
        assert job["state"] == "turn-completed"
        assert bridge._pid_alive(manager["pid"])
        time.sleep(0.08)
        assert bridge._pid_alive(manager["pid"])

        _wait_for_pid_exit(manager["pid"], timeout=8.0)
        assert not bridge._pid_alive(manager["pid"])
    finally:
        release_worker.touch()
        _wait_for_pid_exit(manager["pid"], timeout=8.0)


def test_manager_failed_start_removes_record_and_terminates_spawn(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))

    class FailedProcess:
        pid = 987654

        def poll(self):
            return 1

    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *_args, **_kwargs: FailedProcess())

    with pytest.raises(bridge.BridgeError, match="health check"):
        bridge.ensure_manager()

    assert not bridge._manager_record_path().exists()


def test_managed_worker_outlives_start_and_follow_returns_step_start_before_final(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    release_final = tmp_path / "release-final"
    fake_cli = _write_fake_aliyun(
        tmp_path,
        "import json, time\n"
        + "from pathlib import Path\n"
        + "counter = 0\n"
        + "def emit(value):\n"
        + "    global counter\n"
        + "    counter += 1\n"
        + "    print(json.dumps({'id': 'v1.stream.{}'.format(counter), 'data': value}), flush=True)\n"
        + "def status(state, text='', metadata=None):\n"
        + "    body = {'state': state}\n"
        + "    if text:\n"
        + "        body['message'] = {'role': 'ROLE_AGENT', 'parts': [{'text': text}]}\n"
        + "    return {'result': {'statusUpdate': {'taskId': 'task-1', 'contextId': 'session-1', "
        + "'status': body, 'metadata': {'iac_code': metadata or {}, 'iacCodeSessionId': 'iac-1'}}}}\n"
        + "time.sleep(0.35)\n"
        + "emit(status('TASK_STATE_WORKING', metadata={'pipeline': {'eventType': 'step_started', "
        + "'step': {'id': 'intent_parsing', 'name': 'Understand'}}}))\n"
        + "release = Path({!r})\n".format(str(release_final))
        + "deadline = time.monotonic() + 10\n"
        + "while not release.exists() and time.monotonic() < deadline:\n"
        + "    time.sleep(0.05)\n"
        + "emit(status('TASK_STATE_WORKING', 'done', {'assistantFinal': {'complete': True}}))\n"
        + "emit(status('TASK_STATE_INPUT_REQUIRED'))\n",
    )

    started = bridge._start_job_local(
        {
            "workspace": str(workspace),
            "prompt": "创建测试模板",
            "mode": "normal",
            "transport": "aliyun_cli",
            "endpoint": "ros.aliyuncs.com",
            "regionId": "cn-hangzhou",
            "aliyunPath": str(fake_cli),
        }
    )

    assert started["state"] == "submitted"
    assert bridge._pid_alive(started["workerPid"])
    _root, job_path, _spool = bridge._job_paths(started["jobId"])
    assert bridge._load_state_json(job_path)["readTimeout"] == bridge.DEFAULT_READ_TIMEOUT_SECONDS

    first = bridge._follow_job_local(started["jobId"], 0, 3)
    assert first["state"] == "working"
    assert first["boundaryReached"] is True
    assert first["presentationRequired"] is True
    assert first["userUpdates"] == ["步骤开始：Understand"]
    assert first["sessionId"] == "session-1"
    assert "finalText" not in first
    assert bridge._pid_alive(started["workerPid"])

    release_final.touch()
    second = bridge._follow_job_local(started["jobId"], first["cursor"], 3)
    assert second["state"] == "turn-completed"
    assert second["finalText"] == "done"
    _wait_for_pid_exit(started["workerPid"])
    assert not bridge._pid_alive(started["workerPid"])


def test_managed_remote_bootstrap_is_committed_before_ack_and_recovers_lost_result(monkeypatch, tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(state_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    counter_path = tmp_path / "remote-count"
    calls_path = tmp_path / "remote-calls.jsonl"
    observation_path = tmp_path / "ack-observation.json"
    fake_cli = _write_fake_aliyun(
        tmp_path,
        "import json, os, sys, time\n"
        + "from pathlib import Path\n"
        + "counter_path = Path({!r})\n".format(str(counter_path))
        + "calls_path = Path({!r})\n".format(str(calls_path))
        + "observation_path = Path({!r})\n".format(str(observation_path))
        + "count = int(counter_path.read_text(encoding='utf-8') or '0') + 1 if counter_path.exists() else 1\n"
        + "counter_path.write_text(str(count), encoding='utf-8')\n"
        + "with calls_path.open('a', encoding='utf-8') as handle:\n"
        + "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        + "def status(state, text='', final=False):\n"
        + "    body = {'state': state}\n"
        + "    if text:\n"
        + "        body['message'] = {'role': 'ROLE_AGENT', 'parts': [{'text': text}]}\n"
        + "    metadata = {'iac_code': {}, 'iacCodeSessionId': 'iac-remote-1'}\n"
        + "    if final:\n"
        + "        metadata['iac_code']['assistantFinal'] = {'complete': True}\n"
        + "    return {'result': {'statusUpdate': {'taskId': 'task-remote-1', "
        + "'contextId': 'session-remote-1', 'status': body, 'metadata': metadata}}}\n"
        + "first = status('TASK_STATE_WORKING', 'first')\n"
        + "if count == 1:\n"
        + "    invocation = os.environ['ALICLOUD_ROS_AGENT_INVOCATION_ID']\n"
        + "    ack_path = Path(os.environ['ALICLOUD_ROS_AGENT_BOOTSTRAP_ACK_FILE'])\n"
        + "    print(json.dumps({'invocationId': invocation, 'event': "
        + "{'id': 'v1.remote-managed.1', 'data': first}, 'sessionId': 'session-remote-1'}), flush=True)\n"
        + "    deadline = time.monotonic() + 5\n"
        + "    while not ack_path.exists() and time.monotonic() < deadline:\n"
        + "        time.sleep(0.01)\n"
        + "    state_root = Path(os.environ['ALICLOUD_ROS_AGENT_STATE_DIR'])\n"
        + "    job_path = next((state_root / 'jobs').glob('*/job.json'))\n"
        + "    spool_path = job_path.with_name('events.jsonl')\n"
        + "    job = json.loads(job_path.read_text(encoding='utf-8'))\n"
        + "    records = [json.loads(line) for line in spool_path.read_text(encoding='utf-8').splitlines()]\n"
        + "    ack = json.loads(ack_path.read_text(encoding='utf-8')) if ack_path.exists() else {}\n"
        + "    observation_path.write_text(json.dumps({'ack': ack, 'sessionId': job.get('sessionId'), "
        + "'streamCursor': job.get('streamCursor'), 'spoolIds': "
        + "[item.get('streamEventId') for item in records]}), encoding='utf-8')\n"
        + "    print(json.dumps({'code': 'ExecutorTimeout'}), file=sys.stderr)\n"
        + "    raise SystemExit(1)\n"
        + "completed = status('TASK_STATE_COMPLETED', 'done', True)\n"
        + "print(json.dumps([{'id': 'v1.remote-managed.1', 'data': first}, "
        + "{'id': 'v1.remote-managed.2', 'data': completed}]), flush=True)\n",
    )

    started = bridge._start_job_local(
        {
            "workspace": str(workspace),
            "prompt": "safe remote reconnect test",
            "mode": "normal",
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
            "endpoint": "ros-pre.aliyuncs.com",
            "regionId": "cn-hangzhou",
            "aliyunPath": str(fake_cli),
        }
    )
    bridge._drive_foreground_cli(started["jobId"], 0, 10)
    result = bridge._follow_job_local(started["jobId"], 0, 0)

    assert result["state"] == "turn-completed"
    assert result["finalText"] == "done"
    calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert "--query" in calls[0]
    assert "--query" not in calls[1]
    assert "--stream-options" not in calls[1]
    assert _command_body(calls[1]) == {
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.remote-managed.1",
    }
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    assert observation["ack"] == {
        "committed": True,
        "eventId": "v1.remote-managed.1",
        "invocationId": observation["ack"]["invocationId"],
    }
    assert observation["sessionId"] == "session-remote-1"
    assert observation["streamCursor"] == "v1.remote-managed.1"
    assert observation["spoolIds"] == ["v1.remote-managed.1"]
    _wait_for_pid_exit(started["workerPid"])


def test_worker_failed_start_cleans_request_and_marks_job_failed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "0" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "submitted",
            "mode": "normal",
            "activeRequestSeq": 1,
            "turn": 1,
            "artifacts": [],
        },
    )

    def fail_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(bridge.subprocess, "Popen", fail_spawn)
    with pytest.raises(bridge.BridgeError, match="could not be started"):
        bridge._spawn_worker(job_id, {"requestSeq": 1, "prompt": "hello"})

    job = bridge._load_state_json(job_path)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "worker_start_failed"
    assert not list(root.glob("request-*.json"))


def test_follow_timeout_keeps_worker_and_cursor_can_continue(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "zh",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )

    timed_out = bridge._follow_job_local(job_id, 0, 0)
    assert timed_out["followTimedOut"] is True
    assert timed_out["cursor"] == 1
    assert timed_out["milestones"] == []
    assert bridge._pid_alive(os.getpid())

    bridge._append_projection(
        job_id,
        {
            "type": "milestone",
            "state": "working",
            "requestSeq": 1,
            "milestones": [{"eventType": "step_started", "step": {"id": "deploying", "name": "Deploy"}}],
        },
    )
    continued = bridge._follow_job_local(job_id, timed_out["cursor"], 0)
    assert continued["boundaryReached"] is True
    assert "followTimedOut" not in continued
    assert continued["userUpdates"] == ["步骤开始：Deploy"]
    assert continued["cursor"] == 2


def test_repeated_follow_timeouts_advance_cursor_without_remote_progress(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "b" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )

    first = bridge._follow_job_local(job_id, 0, 0)
    second = bridge._follow_job_local(job_id, first["cursor"], 0)

    assert first["followTimedOut"] is True
    assert second["followTimedOut"] is True
    assert (first["cursor"], second["cursor"]) == (1, 2)
    assert first["milestones"] == second["milestones"] == []
    assert "userUpdates" not in first
    assert "userUpdates" not in second
    assert [item["type"] for item in bridge._read_spool(spool)] == ["follow-heartbeat", "follow-heartbeat"]


def test_follow_timeout_does_not_consume_step_boundary_arriving_after_heartbeat(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "c" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )
    follow_timeout_result = bridge._follow_timeout_result

    def record_then_publish_boundary(current_job_id: str, start_cursor: int):
        result = follow_timeout_result(current_job_id, start_cursor)
        bridge._append_projection(
            current_job_id,
            {
                "type": "milestone",
                "state": "working",
                "requestSeq": 1,
                "milestones": [{"eventType": "step_started", "step": {"id": "deploying", "name": "Deploy"}}],
            },
        )
        return result

    monkeypatch.setattr(bridge, "_follow_timeout_result", record_then_publish_boundary)

    timed_out = bridge._follow_job_local(job_id, 0, 0)
    assert timed_out["followTimedOut"] is True
    assert timed_out["cursor"] == 1
    assert timed_out["milestones"] == []
    assert "boundaryReached" not in timed_out
    assert "userUpdates" not in timed_out

    monkeypatch.setattr(bridge, "_follow_timeout_result", follow_timeout_result)
    continued = bridge._follow_job_local(job_id, timed_out["cursor"], 0)
    assert continued["boundaryReached"] is True
    assert continued["cursor"] == 2
    assert continued["userUpdates"] == ["Step started: Deploy"]


@pytest.mark.parametrize("gate", ["terminal", "input-required"])
def test_follow_timeout_snapshot_does_not_skip_step_before_new_gate(monkeypatch, tmp_path: Path, gate: str) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = ("d" if gate == "terminal" else "e") * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )
    follow_timeout_result = bridge._follow_timeout_result

    def record_then_publish_gate(current_job_id: str, start_cursor: int):
        result = follow_timeout_result(current_job_id, start_cursor)
        bridge._append_projection(
            current_job_id,
            {
                "type": "milestone",
                "state": "working",
                "requestSeq": 1,
                "milestones": [{"eventType": "step_completed", "step": {"id": "planning", "name": "Plan"}}],
            },
        )
        if gate == "terminal":
            bridge._finish_job(
                current_job_id,
                1,
                {"state": "completed", "pipelineResult": {"status": "completed"}},
                os.getpid(),
            )
        else:
            bridge._append_projection(
                current_job_id,
                {
                    "type": "input-required",
                    "state": "input-required",
                    "requestSeq": 1,
                    "inputRequired": {
                        "schemaVersion": 1,
                        "kind": "candidate_selection",
                        "inputId": "selection-1",
                    },
                },
            )
        return result

    monkeypatch.setattr(bridge, "_follow_timeout_result", record_then_publish_gate)
    timed_out = bridge._follow_job_local(job_id, 0, 0)

    assert timed_out["state"] == "working"
    assert timed_out["followTimedOut"] is True
    assert timed_out["cursor"] == 1
    assert timed_out["milestones"] == []
    assert "boundaryReached" not in timed_out

    monkeypatch.setattr(bridge, "_follow_timeout_result", follow_timeout_result)
    continued = bridge._follow_job_local(job_id, timed_out["cursor"], 0)
    assert continued["boundaryReached"] is True
    assert continued["userUpdates"] == ["Step completed: Plan"]
    if gate == "terminal":
        assert continued["state"] == "completed"
        assert continued["pipelineResult"] == {"status": "completed"}
        assert continued["cursor"] == 3
    else:
        assert continued["state"] == "input-required"
        assert continued["inputRequired"]["inputId"] == "selection-1"
        assert continued["cursor"] == 3


@pytest.mark.parametrize("gate", ["terminal", "input-required"])
def test_follow_observation_rechecks_step_and_gate_arriving_after_snapshot(
    monkeypatch, tmp_path: Path, gate: str
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = ("1" if gate == "terminal" else "2") * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )
    follow_ready_result = bridge._follow_ready_result
    injected = False

    def snapshot_then_publish_gate(current_job_id: str, start_cursor: int):
        nonlocal injected
        result = follow_ready_result(current_job_id, start_cursor)
        if injected:
            return result
        injected = True
        bridge._append_projection(
            current_job_id,
            {
                "type": "milestone",
                "state": "working",
                "requestSeq": 1,
                "milestones": [{"eventType": "step_completed", "step": {"id": "planning", "name": "Plan"}}],
            },
        )
        if gate == "terminal":
            bridge._finish_job(
                current_job_id,
                1,
                {"state": "completed", "pipelineResult": {"status": "completed"}},
                os.getpid(),
            )
        else:
            bridge._append_projection(
                current_job_id,
                {
                    "type": "input-required",
                    "state": "input-required",
                    "requestSeq": 1,
                    "inputRequired": {
                        "schemaVersion": 1,
                        "kind": "candidate_selection",
                        "inputId": "selection-1",
                    },
                },
            )
        return result

    monkeypatch.setattr(bridge, "_follow_ready_result", snapshot_then_publish_gate)
    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["boundaryReached"] is True
    assert result["userUpdates"] == ["Step completed: Plan"]
    assert "followTimedOut" not in result
    if gate == "terminal":
        assert result["state"] == "completed"
        assert result["pipelineResult"] == {"status": "completed"}
        assert result["cursor"] == 2
    else:
        assert result["state"] == "input-required"
        assert result["inputRequired"]["inputId"] == "selection-1"
        assert result["cursor"] == 2


@pytest.mark.parametrize("exit_kind", ["finished", "stopped"])
@pytest.mark.parametrize(("role", "request_seq", "token"), [
    ("primary", 1, "current"),
    ("primary", 0, "current"),
    ("sideband", 1, "current"),
    ("sideband", 0, "current"),
    ("sideband", 1, "old"),
])
def test_worker_exit_clears_launcher_pid_only_for_owned_request(
    monkeypatch, tmp_path: Path, exit_kind, role, request_seq, token,
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(bridge, "_touch_manager_activity", lambda: None)
    job_id = "3" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    original = {
        "jobId": job_id,
        "state": "working",
        "activeRequestSeq": 1,
        "workerPid": 101,
        "sidebandWorkerPid": 202,
        "sidebandWorkerToken": "current",
    }
    bridge._atomic_json(job_path, original)

    # Actual worker PID differs from both launcher PIDs persisted by Popen.
    if exit_kind == "stopped":
        bridge._record_stopped_worker_exit(job_id, request_seq, role, token, 303)
    elif role == "sideband":
        bridge._finish_sideband_job(job_id, request_seq, token, {"state": "stream-ended"}, 303)
    else:
        bridge._finish_job(job_id, request_seq, {"state": "completed"}, 303)

    job = bridge._load_state_json(job_path)
    if request_seq != 1 or (role == "sideband" and token != "current"):
        assert job == original
    elif role == "sideband":
        assert "sidebandWorkerPid" not in job
        assert "sidebandWorkerToken" not in job
        assert job["workerPid"] == 101
        assert job["state"] == "working"
    else:
        assert "workerPid" not in job
        assert job["sidebandWorkerPid"] == 202
        assert job["sidebandWorkerToken"] == "current"


@pytest.mark.parametrize("expected_worker_pid", [0, 999])
def test_observer_cannot_finish_a_replaced_launcher(monkeypatch, tmp_path: Path, expected_worker_pid) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "3" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    original = {"state": "working", "activeRequestSeq": 1, "workerPid": 101}
    bridge._atomic_json(job_path, original)

    assert not bridge._finish_job(
        job_id, 1, {"state": "failed"}, 303, expected_worker_pid=expected_worker_pid,
    )
    assert bridge._load_state_json(job_path) == original


def test_follow_dead_worker_check_does_not_overwrite_concurrent_completion(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "3" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": 987654,
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )
    checked = False

    def complete_then_report_dead(pid: int) -> bool:
        nonlocal checked
        assert pid == 987654
        if checked:
            return False
        checked = True
        bridge._append_projection(
            job_id,
            {
                "type": "milestone",
                "state": "working",
                "requestSeq": 1,
                "milestones": [{"eventType": "step_completed", "step": {"id": "deploying", "name": "Deploy"}}],
            },
        )
        assert bridge._finish_job(
            job_id,
            1,
            {"state": "completed", "pipelineResult": {"status": "completed"}},
            987654,
        )
        return False

    monkeypatch.setattr(bridge, "_pid_alive", complete_then_report_dead)
    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["state"] == "completed"
    assert result["pipelineResult"] == {"status": "completed"}
    assert result["userUpdates"] == ["Step completed: Deploy"]
    assert result["cursor"] == 2
    assert "error" not in result
    persisted = bridge._load_state_json(job_path)
    assert persisted["state"] == "completed"
    assert "error" not in persisted
    assert [item["state"] for item in bridge._read_spool(spool) if item["type"] == "result-boundary"] == ["completed"]


def test_follow_sideband_dead_check_does_not_consume_parent_step_after_concurrent_ack(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "4" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permission = {
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": "sub_pipeline",
        "decision": "allow_once",
    }
    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
        "accepted": True,
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "sidebandWorkerPid": 987654,
            "sidebandWorkerToken": "worker-token",
            "sidebandResponse": permission,
            "lastPermissionResponse": {
                "inputId": permission["inputId"],
                "toolUseId": permission["toolUseId"],
                "decision": "allow_once",
            },
            "sidebandResponseInputId": permission["inputId"],
            "pendingPermissions": [permission],
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )
    checked = False

    def acknowledge_then_report_dead(pid: int) -> bool:
        nonlocal checked
        if pid == os.getpid():
            return True
        assert pid == 987654
        if checked:
            return False
        checked = True
        bridge._finish_sideband_job(
            job_id,
            1,
            "worker-token",
            {"ok": True, "state": "permission-responded", "permissionAck": acknowledgement},
            987654,
        )
        bridge._append_projection(
            job_id,
            {
                "type": "milestone",
                "state": "working",
                "requestSeq": 1,
                "milestones": [{"eventType": "step_completed", "step": {"id": "evaluating", "name": "Evaluate"}}],
            },
        )
        return False

    monkeypatch.setattr(bridge, "_pid_alive", acknowledge_then_report_dead)
    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["state"] == "working"
    assert result["boundaryReached"] is True
    assert result["userUpdates"] == ["Step completed: Evaluate"]
    assert result["cursor"] == 1
    assert "followTimedOut" not in result
    job = bridge._load_state_json(job_path)
    assert job["permissionAck"] == acknowledgement
    assert "sidebandWorkerToken" not in job
    assert "sidebandError" not in job


def test_follow_does_not_treat_permission_ack_as_pipeline_boundary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "7" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "permission-responded",
            "mode": "pipeline",
            "preferredLanguage": "zh",
            "activeRequestSeq": 2,
            "workerPid": os.getpid(),
            "permissionAck": {"kind": "permission_ack", "accepted": True},
            "createdAt": int(time.time()),
            "turn": 1,
            "artifacts": [],
        },
    )

    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["state"] == "permission-responded"
    assert result["followTimedOut"] is True
    assert result["presentationRequired"] is True


def test_follow_reports_detached_stream_after_permission_ack(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "8" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "permission-responded",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 2,
            "permissionAck": {"kind": "permission_ack", "accepted": True},
            "turn": 1,
            "artifacts": [],
        },
    )

    result = bridge._follow_job_local(job_id, 0, 1)

    assert result["state"] == "failed"
    assert result["error"]["code"] == "stream_detached"
    assert result["presentationRequired"] is True


def test_managed_job_ignores_stale_permission_projection_after_ack(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "f" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
        "accepted": True,
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "permissionAck": acknowledgement,
            "artifacts": [],
        },
    )

    bridge._append_projection(
        job_id,
        {
            "type": "input-required",
            "state": "input-required",
            "requestSeq": 1,
            "inputRequired": {
                "kind": "permission",
                "inputId": "permission-1",
                "toolUseId": "tool-1",
                "toolName": "write_file",
                "permissionClass": "pipeline",
            },
        },
    )

    job = bridge._load_state_json(job_path)
    assert job["state"] == "working"
    assert "inputRequired" not in job
    assert spool.read_text(encoding="utf-8") == ""


def test_follow_includes_already_queued_step_boundaries_with_existing_input(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "b" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    pending = {
        "kind": "candidate_selection",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "candidate-1",
        "prompt": "Choose",
        "options": [{"id": "one", "label": "One"}],
    }
    events = [
        {
            "type": "milestone",
            "requestSeq": 1,
            "milestones": [{"eventType": "step_started", "step": {"id": "intent_parsing"}}],
        },
        {
            "type": "milestone",
            "requestSeq": 1,
            "milestones": [{"eventType": "step_completed", "step": {"id": "intent_parsing"}}],
        },
        {"type": "input-required", "requestSeq": 1, "inputRequired": pending},
    ]
    spool.write_text("".join(json.dumps(value) + "\n" for value in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "input-required",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "inputRequired": pending,
            "artifacts": [],
        },
    )

    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["boundaryReached"] is True
    assert result["cursor"] == 3
    assert result["state"] == "input-required"
    assert result["inputRequired"] == pending
    assert result["userUpdates"] == [
        "Step started: intent_parsing",
        "Step completed: intent_parsing",
    ]


def test_follow_reports_dead_worker_instead_of_waiting_forever(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "c" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "normal",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": 99999999,
            "turn": 1,
            "artifacts": [],
        },
    )

    result = bridge._follow_job_local(job_id, 0, 0)
    assert result["state"] == "failed"
    assert result["error"]["code"] == "worker_exited"
    assert result["presentationRequired"] is True


def test_remote_failed_status_promotes_safe_text_to_diagnostic_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "9" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "normal",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "artifacts": [],
        },
    )

    bridge._finish_job(
        job_id,
        1,
        {"ok": False, "state": "failed", "latestText": "Invalid A2A workspace metadata."},
        0,
    )
    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["state"] == "failed"
    assert result["error"] == {
        "code": "remote_task_failed",
        "message": "Invalid A2A workspace metadata.",
    }
    assert result["presentationRequired"] is True


def test_candidate_projection_keeps_architecture_and_cost_details() -> None:
    value = {
        "schemaVersion": 1,
        "kind": "candidate_selection",
        "requestTaskId": "task-1",
        "contextId": "ctx-1",
        "inputId": "candidate-1",
        "prompt": "Choose",
        "options": [
            {
                "id": "one",
                "label": "Plan One",
                "summary": "Existing VPC with one VSwitch",
                "architectureDiagram": "flowchart LR\n  VPC --> VSwitch",
                "totalMonthlyCost": "0 CNY/month",
                "costItems": [{"name": "VSwitch", "spec": "standard", "monthlyCost": "0 CNY/month"}],
            }
        ],
    }

    projected = bridge._safe_input(value)
    assert projected is not None
    assert projected["options"][0]["architectureDiagram"].startswith("flowchart LR")
    assert projected["options"][0]["totalMonthlyCost"] == "0 CNY/month"
    assert projected["options"][0]["costItems"][0]["name"] == "VSwitch"


def test_step_updates_include_bounded_conclusion_and_candidate_coordinate() -> None:
    intent = bridge._safe_milestone(
        {
            "eventType": "step_completed",
            "step": {"id": "intent_parsing", "name": "Understand requirements"},
            "data": {
                "conclusionField": "intent",
                "conclusion": {
                    "user_message_summary": "在已有 VPC 中部署一个新 VSwitch",
                    "non_functional": {"region_preference": "cn-hangzhou"},
                    "resource_intents": [
                        {"product": "VPC", "action": "use_existing"},
                        {"product": "VSwitch", "action": "create"},
                    ],
                },
            },
        }
    )
    assert intent is not None
    update = bridge._format_user_update(intent, "zh")
    assert update.startswith("步骤完成：Understand requirements")
    assert "结论" in update
    assert "VPC (复用)" in update
    assert "VSwitch (新建)" in update

    candidate = bridge._format_user_update(
        {
            "eventType": "candidate_step_started",
            "candidate": {"id": "candidate-a", "name": "低成本方案"},
            "candidateStep": {"id": "cost", "name": "成本估算", "index": 2, "total": 3},
        },
        "zh",
    )
    assert candidate == "候选步骤开始：低成本方案 · 2/3 成本估算"


def test_step_boundary_can_include_authoritative_result_and_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "1" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    artifact = {"name": "template.yaml", "uri": "file:///workspace/template.yaml"}
    events = [
        {
            "type": "milestone",
            "requestSeq": 1,
            "milestones": [{"eventType": "step_completed", "step": {"id": "reviewing"}}],
        },
        {"type": "result-boundary", "requestSeq": 1, "state": "turn-completed"},
    ]
    spool.write_text("".join(json.dumps(value) + "\n" for value in events), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "turn-completed",
            "mode": "normal",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "finalText": "done",
            "finalTextComplete": True,
            "artifacts": [artifact],
        },
    )

    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["boundaryReached"] is True
    assert result["state"] == "turn-completed"
    assert result["finalText"] == "done"
    assert result["artifacts"] == [artifact]


def test_working_step_boundary_does_not_repeat_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "5" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    artifact = {"name": "template.yaml", "uri": "file:///workspace/template.yaml"}
    spool.write_text(
        json.dumps(
            {
                "type": "milestone",
                "requestSeq": 1,
                "milestones": [{"eventType": "step_completed", "step": {"id": "reviewing"}}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "turn": 1,
            "artifacts": [artifact],
        },
    )

    result = bridge._follow_job_local(job_id, 0, 0)

    assert result["boundaryReached"] is True
    assert result["state"] == "working"
    assert "artifacts" not in result


@pytest.mark.parametrize("kind", ["ask_user_question", "candidate_selection"])
def test_managed_continue_uses_natural_language_for_business_input(monkeypatch, tmp_path: Path, kind: str) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = ("2" if kind == "ask_user_question" else "3") * 32
    workspace = tmp_path / kind
    workspace.mkdir()
    answer = workspace / "answer.txt"
    answer.write_text("使用 cn-hangzhou-h 区的第一个方案", encoding="utf-8")
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "input-required",
            "mode": "pipeline",
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "zh",
            "activeRequestSeq": 1,
            "turn": 1,
            "inputRequired": {"kind": kind, "inputId": "input-1"},
            "artifacts": [],
        },
    )
    captured = {}

    def spawn(_job_id, request):
        captured.update(request)
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)
    result = bridge._continue_job_local({"jobId": job_id, "promptFile": str(answer)})

    assert result["state"] == "submitted"
    assert captured["prompt"] == "使用 cn-hangzhou-h 区的第一个方案"
    assert not captured["prompt"].startswith(bridge.PERMISSION_QUERY_PREFIX)


def test_managed_continue_reuses_completed_pipeline_normal_handoff(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "4" * 32
    workspace = tmp_path / "pipeline-handoff"
    workspace.mkdir()
    prompt = workspace / "next.txt"
    prompt.write_text("删除刚部署的资源栈", encoding="utf-8")
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "completed",
            "mode": "pipeline",
            "conversationMode": "normal",
            "normalHandoffReady": True,
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-pipeline-1",
            "taskId": "task-pipeline-1",
            "preferredLanguage": "zh",
            "activeRequestSeq": 1,
            "turn": 1,
            "pipelineResult": {"status": "success", "stack_id": "stack-1"},
            "artifacts": [],
        },
    )
    captured = {}

    def spawn(captured_job_id, request):
        captured["jobId"] = captured_job_id
        captured["request"] = request
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)

    result = bridge._continue_job_local({"jobId": job_id, "promptFile": str(prompt)})

    assert result["jobId"] == job_id
    assert result["mode"] == "pipeline"
    assert result["conversationMode"] == "normal"
    assert result["sessionId"] == "session-pipeline-1"
    assert result["turn"] == 2
    assert captured["jobId"] == job_id
    assert captured["request"]["mode"] == "pipeline"
    assert captured["request"]["summaryMode"] == "normal"
    assert captured["request"]["sessionId"] == "session-pipeline-1"
    job = bridge._load_state_json(job_path)
    assert job["taskHistory"] == ["task-pipeline-1"]
    assert "taskId" not in job
    assert "pipelineResult" not in job


@pytest.mark.parametrize(
    ("stop_status", "result_state", "persisted_state", "ok"),
    [
        ("Stopped", "canceled", "canceled", True),
        ("Stopping", "canceling", "canceling", True),
        ("NoActiveStream", "not-active", "not-active", True),
        ("Failed", "cancel-failed", "failed", False),
    ],
)
def test_cancel_managed_job_calls_stop_chat_and_preserves_authoritative_state(
    monkeypatch,
    tmp_path: Path,
    stop_status: str,
    result_state: str,
    persisted_state: str,
    ok: bool,
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "6" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.write_text('{"type":"milestone","requestSeq":1,"milestones":[]}\n', encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(tmp_path),
            "state": "working",
            "mode": "pipeline",
            "conversationMode": "pipeline",
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "zh",
            "activeRequestSeq": 1,
            "turn": 1,
            "inputRequired": {"kind": "permission"},
            "artifacts": [],
        },
    )
    captured = {}

    def stop_chat(job, session_id):
        captured["job"] = job
        captured["sessionId"] = session_id
        return {"status": stop_status, "sessionId": session_id, "requestId": "request-1"}

    monkeypatch.setattr(bridge, "_run_stop_chat", stop_chat)

    result = bridge._cancel_job_local({"jobId": job_id})

    assert captured["sessionId"] == "session-1"
    assert captured["job"]["endpoint"] == "ros.aliyuncs.com"
    assert result["ok"] is ok
    assert result["state"] == result_state
    assert result["stopStatus"] == stop_status
    assert result["cursor"] == 1
    assert result["presentationRequired"] is True
    job = bridge._load_state_json(job_path)
    assert job["state"] == persisted_state
    assert job["stopStatus"] == stop_status
    if stop_status == "Stopped":
        assert "inputRequired" not in job
    else:
        assert "inputRequired" not in job or stop_status == "Failed"

    # Follow must preserve the cancellation acknowledgement, not present the
    # old StartChat wire state as a fresh observation of the cloud task.
    followed = bridge._follow_job_local(job_id, result["cursor"], 120)
    assert followed["state"] == persisted_state
    assert followed["stopStatus"] == stop_status
    assert followed["stopRequestId"] == "request-1"
    assert followed["presentationRequired"] is True
    assert "followTimedOut" not in followed


def test_parser_exposes_managed_commands_without_synchronous_chat() -> None:
    parser = bridge.build_parser()
    start = parser.parse_args(["start", "--prompt-file", "/workspace/prompt.txt"])
    follow = parser.parse_args(["follow", "--job-id", "a" * 32, "--cursor", "4"])
    continued = parser.parse_args(["continue", "--job-id", "a" * 32, "--prompt-file", "/workspace/next.txt"])
    respond = parser.parse_args(["respond", "--job-id", "a" * 32, "--permission-ref", "p-1234", "--decision", "deny"])
    cancel = parser.parse_args(["cancel", "--job-id", "a" * 32])

    assert start.command == "start"
    assert start.read_timeout == bridge.DEFAULT_READ_TIMEOUT_SECONDS == 1800
    assert follow.wait_seconds == bridge.DEFAULT_FOLLOW_SECONDS
    assert continued.command == "continue"
    assert respond.command == "respond"
    assert respond.input_file is None
    assert respond.permission_ref == "p-1234"
    assert cancel.command == "cancel"
    choices = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices
    assert "chat" not in choices
    assert "cancel" in choices

    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--cwd", "/workspace", "--prompt-file", "/workspace/prompt.txt"])


def test_request_from_legacy_job_uses_current_stream_read_timeout_default() -> None:
    request = bridge._request_from_job(
        {
            "activeRequestSeq": 2,
            "workspace": "/workspace",
            "mode": "pipeline",
            "endpoint": "ros.aliyuncs.com",
        },
        "continue",
    )

    assert request["readTimeout"] == bridge.DEFAULT_READ_TIMEOUT_SECONDS == 1800
    assert request["transport"] == "aliyun_cli"
    assert request["aliyunCLIExecutionMode"] == "local"


def test_request_from_legacy_remote_job_has_no_transient_environment_allowlist() -> None:
    request = bridge._request_from_job(
        {
            "activeRequestSeq": 2,
            "workspace": "/workspace",
            "mode": "normal",
            "endpoint": "ros.aliyuncs.com",
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
        },
        "continue",
    )

    assert request["aliyunCLIForwardEnv"] == []


def test_remote_cli_job_persists_execution_mode_without_local_identity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = {}
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/remote/bin/aliyun")

    def fake_spawn(job_id, request):
        captured["jobId"] = job_id
        captured["request"] = request
        return 123

    monkeypatch.setattr(bridge, "_spawn_worker", fake_spawn)

    started = bridge._start_job_local(
        {
            "workspace": str(workspace),
            "prompt": "create a VPC",
            "mode": "normal",
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
            "aliyunCLIForwardEnv": ["STAROPS_HIL"],
            "debug": True,
            "endpoint": "ros-pre.aliyuncs.com",
            "regionId": None,
            "profile": None,
            "credentialSource": "remote",
            "aliyunPath": "aliyun",
            "transientEnvironment": {"STAROPS_HIL": "http://fresh.example/hil"},
        }
    )

    _root, job_path, _spool = bridge._job_paths(started["jobId"])
    job = bridge._load_state_json(job_path)
    assert job["aliyunCLIExecutionMode"] == "remote"
    assert job["aliyunCLIForwardEnv"] == ["STAROPS_HIL"]
    assert job["debug"] is True
    assert captured["request"]["debug"] is True
    assert "fresh.example" not in json.dumps(job)
    assert job["profile"] is None
    assert job["regionId"] is None
    assert captured["request"]["aliyunCLIExecutionMode"] == "remote"
    assert captured["request"]["credentialSource"] == "remote"
    assert captured["request"]["_transientEnvironment"] == {"STAROPS_HIL": "http://fresh.example/hil"}


def test_remote_cli_refreshes_starops_hil_across_manager_proxy_and_buffered_pop_cli(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    pop_dir = tmp_path / "pop-cli"
    front_dir = tmp_path / "front-cli"
    workspace = tmp_path / "workspace"
    pop_dir.mkdir()
    front_dir.mkdir()
    workspace.mkdir()
    prompt = workspace / "prompt.txt"
    prompt.write_text("create a test VPC", encoding="utf-8")

    working = _status_event(state="TASK_STATE_WORKING", text="working")
    completed = _status_event(
        state="TASK_STATE_COMPLETED",
        text="remote route refreshed",
        metadata={"assistantFinal": {"complete": True}},
    )
    pop_cli = _write_fake_aliyun(
        pop_dir,
        """import json
import sys

if sys.argv[1:3] != ["ros", "start-chat"]:
    print("unexpected POP CLI command", file=sys.stderr)
    raise SystemExit(2)
print(json.dumps(json.loads(%r), ensure_ascii=False, indent=2))
"""
        % json.dumps(
            [
                {"id": "v1.remote-stream.1", "data": working},
                {"id": "v1.remote-stream.2", "data": completed},
            ],
            ensure_ascii=False,
        ),
    )
    front_cli = _write_fake_aliyun(
        front_dir,
        """import json
import os
import sys
import urllib.error
import urllib.request

route = os.environ.get("STAROPS_HIL", "")
request = urllib.request.Request(
    route,
    data=json.dumps({"args": sys.argv[1:]}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        sys.stdout.buffer.write(response.read())
except (OSError, urllib.error.URLError) as exc:
    print("STAROPS_HIL proxy failed: {}".format(exc), file=sys.stderr)
    raise SystemExit(1)
""",
    )
    calls = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            calls.append(payload)
            result = subprocess.run(
                [str(pop_cli), *payload["args"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.send_response(200 if result.returncode == 0 else 502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(result.stdout)))
            self.end_headers()
            self.wfile.write(result.stdout)

        def log_message(self, _format: str, *_args) -> None:
            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        monkeypatch.setenv("STAROPS_HIL", "http://127.0.0.1:1/stale")
        bridge.ensure_manager(2)
        fresh_route = "http://127.0.0.1:{}/invoke".format(proxy.server_address[1])
        monkeypatch.setenv("STAROPS_HIL", fresh_route)
        monkeypatch.chdir(workspace)
        args = bridge.build_parser().parse_args(
            ["start", "--prompt-file", str(prompt), "--aliyun-path", str(front_cli)]
        )
        bridge.apply_skill_config(
            args,
            {
                "transport": "aliyun_cli",
                "aliyunCLIExecutionMode": "remote",
                "aliyunCLIForwardEnv": ["STAROPS_HIL"],
                "debug": True,
                "endpoint": "ros.aliyuncs.com",
                "managerIdleSeconds": 2,
            },
        )

        started = bridge.run_start_job(args)
        result = bridge.run_follow_job(
            argparse.Namespace(job_id=started["jobId"], cursor=0, wait_seconds=10, manager_idle_seconds=2)
        )

        assert result["state"] == "turn-completed"
        assert result["finalText"] == "remote route refreshed"
        debug_path = workspace / ".ros-agent-debug" / "remote-aliyun-results.jsonl"
        # CLI results precede asynchronous bulk diagnostics. Wait for their
        # documented completion marker before asserting the complete JSONL.
        deadline = time.monotonic() + 5
        while True:
            try:
                debug_records = [json.loads(line) for line in debug_path.read_text(encoding="utf-8").splitlines()]
            except (OSError, json.JSONDecodeError):
                if time.monotonic() >= deadline:
                    raise
            else:
                if any(item["kind"] == "debug-complete" for item in debug_records) and any(
                    item["kind"] == "parsed-event" for item in debug_records
                ):
                    break
                assert time.monotonic() < deadline, debug_records
            time.sleep(0.02)
        assert debug_records[0]["jobId"] == started["jobId"]
        assert debug_records[0]["requestSeq"] == 1
        assert any(item["kind"] == "parsed-event" and item["idPresent"] for item in debug_records)
        assert fresh_route not in debug_path.read_text(encoding="utf-8")
        assert len(calls) == 1
        assert calls[0]["args"][:3] == ["ros", "start-chat", "--endpoint"]
        endpoint_index = calls[0]["args"].index("--endpoint")
        assert calls[0]["args"][endpoint_index + 1] == "ros.aliyuncs.com"
        job_root, job_path, _spool = bridge._job_paths(started["jobId"])
        assert fresh_route not in job_path.read_text(encoding="utf-8")
        assert all(fresh_route not in path.read_text(encoding="utf-8", errors="replace") for path in job_root.iterdir())
    finally:
        proxy.shutdown()
        proxy.server_close()
        proxy_thread.join(timeout=2)


@pytest.mark.parametrize("remote", [False, True])
def test_spawn_worker_keeps_remote_authorization_out_of_parser_environment(
    monkeypatch, tmp_path: Path, remote: bool
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("STAROPS_HIL", "http://stale.example/hil")
    job_id = "7" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    bridge._atomic_json(
        job_path,
        {"schemaVersion": 1, "jobId": job_id, "state": "submitted", "activeRequestSeq": 1, "artifacts": []},
    )
    captured = {}

    class Process:
        pid = 4321

    def popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    request = {
        "requestSeq": 1,
        "prompt": "hello",
        "transport": "aliyun_cli",
        "aliyunCLIExecutionMode": "remote" if remote else "local",
        "aliyunCLIForwardEnv": ["STAROPS_HIL"],
        "_transientEnvironment": {"STAROPS_HIL": "http://fresh.example/hil"},
    }

    bridge._spawn_worker(job_id, request)

    if remote:
        assert "STAROPS_HIL" not in captured["env"]
    else:
        assert captured["env"]["STAROPS_HIL"] == "http://fresh.example/hil"
    persisted_request = next(root.glob("request-*.json")).read_text(encoding="utf-8")
    assert "fresh.example" not in persisted_request
    assert "stale.example" not in persisted_request


@pytest.mark.parametrize(
    ("mode", "permission_class", "decision"),
    [
        ("normal", "normal", "allow_once"),
        ("normal", "normal", "deny"),
        ("pipeline", "pipeline", "allow_once"),
        ("pipeline", "pipeline", "deny"),
    ],
)
def test_managed_respond_preserves_serial_permission_correlation(
    monkeypatch, tmp_path: Path, mode: str, permission_class: str, decision: str
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = ("d" if mode == "normal" else "e") * 32
    workspace = tmp_path / mode
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    pending = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": permission_class,
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "input-required",
            "mode": mode,
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "inputRequired": pending,
            "artifacts": [],
        },
    )
    captured = {}

    def spawn(captured_job_id, request):
        captured["jobId"] = captured_job_id
        captured["request"] = request
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)
    result = bridge._respond_job_local({"jobId": job_id, "decision": decision})

    assert result["permissionResponse"]["decision"] == decision
    assert captured["jobId"] == job_id
    assert captured["request"]["prompt"].startswith(bridge.PERMISSION_QUERY_PREFIX + " ")
    query = json.loads(captured["request"]["prompt"][len(bridge.PERMISSION_QUERY_PREFIX) :])
    assert query["inputId"] == pending["inputId"]
    assert query["toolUseId"] == pending["toolUseId"]
    assert query["decision"] == decision


@pytest.mark.parametrize(
    ("mode", "permission_class"),
    [("normal", "normal"), ("pipeline", "pipeline")],
)
def test_managed_permission_backup_not_ready_remains_retryable(
    monkeypatch, tmp_path: Path, mode: str, permission_class: str
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = ("1" if mode == "normal" else "2") * 32
    workspace = tmp_path / mode
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    pending = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": permission_class,
    }
    response = {
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "submitted",
            "mode": mode,
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "en",
            "activeRequestSeq": 2,
            "workerPid": 12345,
            "turn": 1,
            "lastPermissionResponse": response,
            "permissionResponseInput": pending,
            "artifacts": [],
        },
    )
    error = {
        "code": bridge.SESSION_BACKUP_NOT_READY_CODE,
        "message": "Session backup is still synchronizing. Retry after 3 seconds.",
        "retryable": True,
    }
    payload = {
        "object": "response",
        "status": "failed",
        "error": {
            "code": -32602,
            "message": error["message"],
            "data": {"code": bridge.SESSION_BACKUP_NOT_READY_CODE, "retryable": True},
        },
    }
    summary = bridge.StreamSummary("session-1", mode=mode)
    summary.apply(payload)
    bridge._append_projection(
        job_id,
        bridge._project_managed_stream_event(payload, summary, mode, 2, "primary", None),
    )
    projected = bridge._load_state_json(job_path)
    assert projected["state"] == "input-required"
    assert projected["inputRequired"] == pending
    assert bridge._read_spool(spool)[-1]["type"] == "input-required"

    assert bridge._finish_job(
        job_id,
        2,
        summary.to_result(0, ""),
        12345,
    )
    waiting = bridge._load_state_json(job_path)
    assert waiting["state"] == "input-required"
    assert waiting["inputRequired"] == pending
    assert waiting["error"] == error
    assert waiting["permissionResponseInput"] == pending
    assert "workerPid" not in waiting

    captured = {}

    def spawn(captured_job_id, request):
        captured["jobId"] = captured_job_id
        captured["request"] = request
        return 23456

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)
    retried = bridge._respond_job_local(
        {
            "jobId": job_id,
            "decision": "allow_once",
            "transientEnvironment": {},
        }
    )

    assert retried["workerPid"] == 23456
    assert captured["jobId"] == job_id
    assert captured["request"]["permissionResponse"] == response
    retried_job = bridge._load_state_json(job_path)
    assert retried_job["state"] == "submitted"
    assert retried_job["permissionResponseInput"] == pending


def test_managed_respond_requires_short_ref_for_multiple_permissions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "b" * 32
    workspace = tmp_path / "multiple"
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permissions = [
        {
            "schemaVersion": 1,
            "kind": "permission",
            "requestTaskId": "task-1",
            "contextId": "session-1",
            "inputId": "permission-{}".format(index),
            "toolUseId": "tool-{}".format(index),
            "permissionClass": "sub_pipeline",
        }
        for index in range(2)
    ]
    original_job = {
        "schemaVersion": 1,
        "jobId": job_id,
        "workspace": str(workspace),
        "state": "input-required",
        "mode": "pipeline",
        "endpoint": "ros.aliyuncs.com",
        "sessionId": "session-1",
        "preferredLanguage": "en",
        "activeRequestSeq": 1,
        "workerPid": 4321,
        "turn": 1,
        "inputRequired": permissions[0],
        "pendingPermissions": permissions,
        "artifacts": [],
    }
    bridge._atomic_json(job_path, original_job)
    monkeypatch.setattr(
        bridge,
        "_spawn_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ambiguous response must not spawn")),
    )

    with pytest.raises(bridge.BridgeError) as error:
        bridge._respond_job_local({"jobId": job_id, "decision": "allow_once"})

    assert error.value.code == "permission_selection_required"
    assert bridge._load_state_json(job_path) == original_job


def test_managed_respond_runs_sub_pipeline_permission_beside_live_parent_worker(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "6" * 32
    workspace = tmp_path / "pipeline"
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permissions = []
    for index in range(2):
        permissions.append(
            {
                "schemaVersion": 1,
                "kind": "permission",
                "requestTaskId": "task-1",
                "contextId": "session-1",
                "inputId": "permission-{}".format(index + 1),
                "toolUseId": "tool-{}".format(index + 1),
                "permissionClass": "sub_pipeline",
            }
        )
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "input-required",
            "mode": "pipeline",
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": 4321,
            "turn": 1,
            "inputRequired": permissions[0],
            "pendingPermissions": permissions,
            "artifacts": [],
        },
    )
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: pid == 4321)
    captured = {}

    def spawn(captured_job_id, request):
        captured["jobId"] = captured_job_id
        captured["request"] = request
        return 12345

    monkeypatch.setattr(bridge, "_spawn_worker", spawn)

    result = bridge._respond_job_local(
        {
            "jobId": job_id,
            "permissionRef": bridge._permission_ref(permissions[0]),
            "decision": "allow_once",
        }
    )
    job = bridge._load_state_json(job_path)

    assert result["workerPid"] == 12345
    assert captured["jobId"] == job_id
    assert captured["request"]["workerRole"] == "sideband"
    assert captured["request"]["requestSeq"] == 1
    worker_token = captured["request"]["workerToken"]
    assert job["workerPid"] == 4321
    assert job["activeRequestSeq"] == 1
    assert job["state"] == "working"
    assert job["pendingPermissions"] == [permissions[1]]
    assert job["inputRequired"] == permissions[1]
    assert job["sidebandResponseInputId"] == permissions[0]["inputId"]

    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": permissions[0]["inputId"],
        "toolUseId": permissions[0]["toolUseId"],
        "decision": "allow_once",
        "accepted": True,
    }
    bridge._append_projection(
        job_id,
        {
            "type": "permission-ack",
            "state": "permission-responded",
            "requestSeq": 1,
            "workerRole": "sideband",
            "workerToken": worker_token,
            "permissionAck": acknowledgement,
        },
    )
    bridge._finish_sideband_job(
        job_id,
        1,
        worker_token,
        {"ok": True, "state": "permission-responded", "permissionAck": acknowledgement},
        12345,
    )
    job = bridge._load_state_json(job_path)

    assert job["workerPid"] == 4321
    assert job["activeRequestSeq"] == 1
    assert job["state"] == "input-required"
    assert job["inputRequired"] == permissions[1]
    assert job["permissionAck"] == acknowledgement
    assert "sidebandWorkerToken" not in job


def test_sideband_sub_pipeline_terminal_does_not_complete_parent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "9" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permission = {
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": "sub_pipeline",
        "decision": "allow_once",
    }
    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
        "accepted": True,
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "activeRequestSeq": 4,
            "workerPid": 4321,
            "sidebandWorkerPid": 12345,
            "sidebandWorkerToken": "worker-token",
            "sidebandResponse": permission,
            "lastPermissionResponse": {
                "inputId": permission["inputId"],
                "toolUseId": permission["toolUseId"],
                "decision": "allow_once",
            },
            "sidebandResponseInputId": permission["inputId"],
            "inputRequired": permission,
            "pendingPermissions": [permission],
            "artifacts": [],
        },
    )

    bridge._finish_sideband_job(
        job_id,
        4,
        "worker-token",
        {
            "ok": True,
            "state": "completed",
            "permissionAck": acknowledgement,
            "pipelineResult": {"child": "only"},
            "normalHandoffReady": True,
        },
        12345,
    )
    job = bridge._load_state_json(job_path)

    assert job["state"] == "working"
    assert job["workerPid"] == 4321
    assert "pipelineResult" not in job
    assert "normalHandoffReady" not in job
    assert "conversationMode" not in job
    assert "inputRequired" not in job
    assert "pendingPermissions" not in job
    assert "sidebandWorkerToken" not in job


def test_sideband_finish_rejects_ack_with_only_a_matching_input_id(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permission = {
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": "sub_pipeline",
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "activeRequestSeq": 1,
            "workerPid": 4321,
            "sidebandWorkerPid": 12345,
            "sidebandWorkerToken": "worker-token",
            "sidebandResponse": permission,
            "lastPermissionResponse": {
                "inputId": "permission-1",
                "toolUseId": "tool-1",
                "decision": "allow_once",
            },
            "artifacts": [],
        },
    )

    bridge._finish_sideband_job(
        job_id,
        1,
        "worker-token",
        {
            "state": "permission-responded",
            "permissionAck": {
                "schemaVersion": 1,
                "kind": "permission_ack",
                "inputId": "permission-1",
                "toolUseId": "tool-other",
                "decision": "allow_once",
                "accepted": True,
            },
        },
        12345,
    )
    job = bridge._load_state_json(job_path)

    assert job["state"] == "input-required"
    assert job["inputRequired"] == permission
    assert job["sidebandError"]["code"] == "permission_not_acknowledged"
    assert "permissionAck" not in job
    assert "acknowledgedPermissionIds" not in job


def test_sideband_finish_does_not_regress_terminal_parent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "8" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permission = {"inputId": "permission-1", "toolUseId": "tool-1", "decision": "allow_once"}
    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
        "accepted": True,
    }
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "completed",
            "mode": "pipeline",
            "activeRequestSeq": 2,
            "sidebandWorkerPid": 12345,
            "sidebandWorkerToken": "worker-token",
            "sidebandResponse": permission,
            "lastPermissionResponse": {
                "inputId": permission["inputId"],
                "toolUseId": permission["toolUseId"],
                "decision": "allow_once",
            },
            "artifacts": [],
        },
    )

    bridge._finish_sideband_job(
        job_id,
        2,
        "worker-token",
        {"ok": True, "state": "permission-responded", "permissionAck": acknowledgement},
        12345,
    )
    job = bridge._load_state_json(job_path)

    assert job["state"] == "completed"
    assert "sidebandWorkerToken" not in job


def test_managed_respond_returns_durable_ack_for_same_duplicate_and_rejects_conflict(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "d" * 32
    workspace = tmp_path / "duplicate-response"
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    permission = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": "normal",
    }
    permission_path = workspace / "permission.json"
    permission_path.write_text(json.dumps(permission), encoding="utf-8")
    response = {
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
    }
    acknowledgement = {
        "schemaVersion": 1,
        "kind": "permission_ack",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "decision": "allow_once",
        "accepted": True,
    }
    bridge._atomic_json(
        job_path,
        {
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "turn-completed",
            "mode": "normal",
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "en",
            "activeRequestSeq": 2,
            "turn": 1,
            "lastPermissionResponse": response,
            "permissionAck": acknowledgement,
            "artifacts": [],
        },
    )
    monkeypatch.setattr(
        bridge,
        "_spawn_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate must not spawn a worker")),
    )

    duplicate = bridge._respond_job_local(
        {"jobId": job_id, "inputFile": str(permission_path), "decision": "allow_once"}
    )

    assert duplicate["state"] == "permission-responded"
    assert duplicate["duplicate"] is True
    assert duplicate["permissionResponse"] == response
    assert duplicate["permissionAck"] == acknowledgement
    with pytest.raises(bridge.BridgeError, match="conflicts with the stored decision"):
        bridge._respond_job_local({"jobId": job_id, "inputFile": str(permission_path), "decision": "deny"})


@pytest.mark.parametrize("suspended", [False, True])
def test_managed_respond_preserves_resident_top_pipeline_parent(monkeypatch, tmp_path: Path, suspended) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "7" * 32
    workspace = tmp_path / "top-pipeline"
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    pending = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": "pipeline",
    }
    permission_path = workspace / "permission.json"
    permission_path.write_text(json.dumps(pending), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "input-required",
            "mode": "pipeline",
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "workerPid": 4321,
            "turn": 1,
            "inputRequired": pending,
            "artifacts": [],
        },
    )
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: pid == 4321)
    requests = []
    monkeypatch.setattr(bridge, "_spawn_worker", lambda _job_id, request: requests.append(request) or 9876)

    if suspended:
        job = bridge._load_state_json(job_path)
        job["permissionWait"] = {"status": "suspended", "resumable": True}
        bridge._atomic_json(job_path, job)
        with pytest.raises(bridge.BridgeError) as error:
            bridge._respond_job_local({"jobId": job_id, "decision": "allow_once"})
        assert error.value.code == "job_busy"
        assert requests == []
        monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: False)
        result = bridge._respond_job_local({"jobId": job_id, "decision": "allow_once"})
        assert result["state"] == "submitted"
        assert bridge._load_state_json(job_path)["activeRequestSeq"] == 2
        assert requests[0].get("workerRole") != "sideband"
        return

    result = bridge._respond_job_local({"jobId": job_id, "inputFile": str(permission_path), "decision": "allow_once"})
    job = bridge._load_state_json(job_path)

    assert result["state"] == "submitted"
    assert job["workerPid"] == 4321
    assert job["activeRequestSeq"] == 1
    assert job["state"] == "working"
    assert job["sidebandResponse"] == pending
    assert requests[0]["workerRole"] == "sideband"
    assert requests[0]["workerToken"] == job["sidebandWorkerToken"]


def test_managed_respond_rejects_sub_pipeline_permission_without_live_parent_stream(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "5" * 32
    workspace = tmp_path / "pipeline-detached"
    workspace.mkdir()
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    pending = {
        "schemaVersion": 1,
        "kind": "permission",
        "requestTaskId": "task-1",
        "contextId": "session-1",
        "inputId": "permission-1",
        "toolUseId": "tool-1",
        "permissionClass": "sub_pipeline",
    }
    permission_path = workspace / "permission.json"
    permission_path.write_text(json.dumps(pending), encoding="utf-8")
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "workspace": str(workspace),
            "state": "input-required",
            "mode": "pipeline",
            "endpoint": "ros.aliyuncs.com",
            "sessionId": "session-1",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "inputRequired": pending,
            "pendingPermissions": [pending],
            "artifacts": [],
        },
    )

    with pytest.raises(bridge.BridgeError) as error:
        bridge._respond_job_local({"jobId": job_id, "inputFile": str(permission_path), "decision": "allow_once"})

    assert error.value.code == "stream_detached"


def test_follow_result_remains_bounded_with_large_final_text(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "f" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "turn-completed",
            "mode": "normal",
            "preferredLanguage": "en",
            "activeRequestSeq": 1,
            "turn": 1,
            "finalText": "结果" * 50000,
            "finalTextComplete": True,
            "artifacts": [],
        },
    )

    result = bridge._job_result(job_id, 0)
    assert len(bridge._json_bytes(result)) <= bridge.MAX_FOLLOW_BYTES
    assert result["finalTextComplete"] is False


def test_reconnect_builders_send_only_the_stream_anchor(monkeypatch) -> None:
    args = _chat_args(session_id="session-1")
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")

    assert bridge.build_reconnect_start_chat_parameters("session-1", "v1.stream.9") == {
        "AgentVersion": "V2",
        "SessionId": "session-1",
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.stream.9",
    }
    command = bridge.build_reconnect_command(args, "session-1", "v1.stream.9")
    assert command[command.index("--session-id") + 1] == "session-1"
    assert command.count("--body") == 1
    assert "--stream-options" not in command
    assert _command_body(command) == {
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.stream.9",
    }
    for forbidden in ("--query", "--biz-mode", "--enable-thinking", "--attachments", "--biz-region-id"):
        assert forbidden not in command


@pytest.mark.parametrize(
    ("event_id", "expected"),
    [
        ("v1.stream.1", ("v1.stream", 1)),
        ("v1.stream.with.dots.42", ("v1.stream.with.dots", 42)),
        ("v1.stream.0002", ("v1.stream", 2)),
    ],
)
def test_stream_event_id_is_parsed_from_the_right_without_rebuilding(event_id, expected) -> None:
    assert bridge.parse_stream_event_id(event_id) == expected


@pytest.mark.parametrize("event_id", [None, "", "1", "v2.stream.1", "v1..1", "v1.stream.0", "v1.stream.x"])
def test_invalid_stream_event_id_is_deterministic(event_id) -> None:
    with pytest.raises(bridge.BridgeError) as error:
        bridge.parse_stream_event_id(event_id)
    assert error.value.code == "invalid_stream_event_id"
    assert error.value.retryable is False


@pytest.mark.parametrize("execution_mode", ["local", "remote"])
def test_cli_reconnect_keeps_cursor_monotonic_during_repeated_head_replay(
    monkeypatch, tmp_path: Path, execution_mode: str
) -> None:
    first = _status_event(state="TASK_STATE_WORKING", text="one")
    second = _status_event(state="TASK_STATE_WORKING", text="two")
    completed = _status_event(
        state="TASK_STATE_COMPLETED", text="done", metadata={"assistantFinal": {"complete": True}}
    )
    attempts = [
        (
            json.dumps(
                [
                    {"id": "v1.stream.with.dots.1", "data": first},
                    {"id": "v1.stream.with.dots.2", "data": second},
                ]
            ),
            1,
        ),
        (json.dumps({"id": "v1.stream.with.dots.1", "data": first}), 1),
        (
            json.dumps(
                [
                    {"id": "v1.stream.with.dots.1", "data": first},
                    {"id": "v1.stream.with.dots.2", "data": second},
                    {"id": "v1.stream.with.dots.3", "data": completed},
                ]
            ),
            0,
        ),
    ]
    commands = []
    invocation_ids = []

    def popen(command, **kwargs):
        output, return_code = attempts.pop(0)
        commands.append(command)
        environment = kwargs.get("env")
        if isinstance(environment, dict):
            invocation_ids.append(environment[bridge.REMOTE_BOOTSTRAP_INVOCATION_ENV])
        if return_code:
            kwargs["stderr"].write(b'{"code":"ReadTimeout"}')
        return _FakeCLIProcess(output, return_code)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    args = _chat_args(
        transport="aliyun_cli",
        aliyun_cli_execution_mode=execution_mode,
        session_id=None,
    )

    result = bridge._consume_start_chat(args, tmp_path, "hello", None, [])

    assert result["state"] == "turn-completed"
    assert result["finalText"] == "done"
    assert result["eventCount"] == 3
    assert len(commands) == 3
    assert "--query" in commands[0]
    for command in commands[1:]:
        assert "--query" not in command
        assert "--stream-options" not in command
        assert _command_body(command) == {
            "StreamOptions.Action": "Reconnect",
            "StreamOptions.Cursor": "v1.stream.with.dots.2",
        }
    if execution_mode == "remote":
        assert len(set(invocation_ids)) == 3


def test_code_reconnect_recovers_terminal_tail_without_resending_query(monkeypatch, tmp_path: Path) -> None:
    terminal = _status_event(state="TASK_STATE_COMPLETED", text="final")
    artifact = {
        "result": {
            "artifactUpdate": {
                "contextId": "session-1",
                "artifact": {
                    "artifactId": "template",
                    "name": "template.yaml",
                    "parts": [{"url": "file:///workspace/template.yaml"}],
                },
            }
        }
    }
    opened = []

    class Response:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self, lines, failure=None):
            self.lines = iter(lines)
            self.failure = failure

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self.lines)
            except StopIteration:
                if self.failure is not None:
                    failure = self.failure
                    self.failure = None
                    raise failure
                raise

        def close(self):
            return None

    responses = [
        Response(
            [
                b"id: v1.tail.1\n",
                ("data: " + json.dumps(terminal) + "\n").encode(),
                b"\n",
            ],
            ConnectionResetError("cut"),
        ),
        Response(
            [
                b"id: v1.tail.2\n",
                ("data: " + json.dumps(artifact) + "\n").encode(),
                b"\n",
            ]
        ),
    ]

    def open_request(_operation, parameters, *_args, **_kwargs):
        opened.append(parameters)
        return responses.pop(0)

    monkeypatch.setattr(bridge, "_open_code_request", open_request)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    args = _chat_args(transport="code", session_id=None)

    result = bridge._consume_start_chat(args, tmp_path, "hello", None, [])

    assert result["state"] == "turn-completed"
    assert result["artifacts"][0]["id"] == "template"
    assert opened[0]["Query"] == "hello"
    assert opened[1] == {
        "AgentVersion": "V2",
        "SessionId": "session-1",
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.tail.1",
    }


def test_remote_bootstrap_is_correlated_committed_and_replayed_idempotently(monkeypatch, tmp_path: Path) -> None:
    first = _status_event(state="TASK_STATE_WORKING", text="first")
    completed = _status_event(
        state="TASK_STATE_COMPLETED", text="done", metadata={"assistantFinal": {"complete": True}}
    )
    calls = []

    def popen(command, **kwargs):
        environment = kwargs["env"]
        invocation_id = environment[bridge.REMOTE_BOOTSTRAP_INVOCATION_ENV]
        calls.append((command, invocation_id, environment[bridge.REMOTE_BOOTSTRAP_ACK_FILE_ENV]))
        if len(calls) == 1:
            wrong = {
                "invocationId": "0" * 32,
                "event": {"id": "v1.wrong.1", "data": first},
                "sessionId": "wrong-session",
            }
            correct = {
                "invocationId": invocation_id,
                "event": {"id": "v1.bootstrap.1", "data": first},
                "sessionId": "session-1",
            }
            kwargs["stderr"].write(b'{"code":"ExecutorTimeout"}')
            return _FakeCLIProcess(json.dumps(wrong) + "\n" + json.dumps(correct), 1)
        output = json.dumps(
            [
                {"id": "v1.bootstrap.1", "data": first},
                {"id": "v1.bootstrap.2", "data": completed},
            ]
        )
        return _FakeCLIProcess(output)

    acknowledgements = []
    original_atomic_json = bridge._atomic_json

    def atomic_json(path, value, mode=0o600):
        if Path(path).name == "bootstrap-ack.json":
            acknowledgements.append(dict(value))
        return original_atomic_json(path, value, mode)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/remote/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge, "_atomic_json", atomic_json)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    args = _chat_args(transport="aliyun_cli", aliyun_cli_execution_mode="remote", session_id=None)

    result = bridge._consume_start_chat(args, tmp_path, "hello", None, [])

    assert result["state"] == "turn-completed"
    assert result["eventCount"] == 2
    assert result["sessionId"] == "session-1"
    assert acknowledgements == [{"invocationId": calls[0][1], "eventId": "v1.bootstrap.1", "committed": True}]
    assert _command_body(calls[1][0]) == {
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.bootstrap.1",
    }
    assert calls[0][1] != calls[1][1]


def test_checkpoint_spools_private_event_id_once_and_keeps_worker_cursors_separate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "1" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "pipeline",
            "activeRequestSeq": 1,
            "sidebandWorkerToken": "sideband-1",
            "artifacts": [],
        },
    )
    primary = {"type": "milestone", "requestSeq": 1, "milestones": [{"eventType": "step_started"}]}
    sideband = dict(primary, workerRole="sideband", workerToken="sideband-1")

    assert bridge._append_projection(job_id, primary, "v1.primary.1") is True
    assert bridge._append_projection(job_id, primary, "v1.primary.1") is True
    assert bridge._append_projection(job_id, sideband, "v1.sideband.1") is True

    records = bridge._read_spool(spool)
    assert [record["streamEventId"] for record in records] == ["v1.primary.1", "v1.sideband.1"]
    job = bridge._load_state_json(job_path)
    assert job["streamCursor"] == "v1.primary.1"
    assert job["sidebandStreamCursor"] == "v1.sideband.1"
    assert "streamEventId" not in bridge._job_result(job_id, 0)


def test_cancel_marks_intent_before_waiting_for_a_delayed_session(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(bridge, "STOP_SESSION_WAIT_SECONDS", 0.0)
    job_id = "2" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "normal",
            "activeRequestSeq": 1,
            "turn": 1,
            "artifacts": [],
        },
    )

    result = bridge._cancel_job_local({"jobId": job_id})

    assert result["state"] == "canceling"
    job = bridge._load_state_json(job_path)
    assert isinstance(job["stopRequestedAt"], int)
    assert job["state"] == "canceling"
    assert "stopDispatchStartedAt" not in job


def test_canceled_worker_only_records_delayed_session_and_stop_is_claimed_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "4" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "normal",
            "activeRequestSeq": 1,
            "stopRequestedAt": 1,
            "artifacts": [],
        },
    )
    projection = {
        "type": "status",
        "requestSeq": 1,
        "sessionId": "session-delayed",
        "latestText": "must not be published",
    }

    assert bridge._append_projection(job_id, projection, "v1.delayed.1") is False

    job = bridge._load_state_json(job_path)
    assert job["sessionId"] == "session-delayed"
    assert "latestText" not in job
    assert "streamCursor" not in job
    assert bridge._read_spool(spool) == []
    claimed = bridge._claim_stop_dispatch(job_id, "session-delayed")
    assert claimed is not None
    assert bridge._claim_stop_dispatch(job_id, "session-delayed") is None


def test_canceled_worker_saves_session_and_stops_even_when_first_business_event_has_no_id(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "5" * 32
    request_token = "6" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "canceling",
            "mode": "normal",
            "activeRequestSeq": 1,
            "workerPid": os.getpid(),
            "stopRequestedAt": 1,
            "artifacts": [],
        },
    )
    bridge._atomic_json(
        root / ("request-{}.json".format(request_token)),
        {
            "requestSeq": 1,
            "workspace": str(tmp_path),
            "prompt": "hello",
            "mode": "normal",
            "summaryMode": "normal",
            "endpoint": "ros.aliyuncs.com",
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "local",
            "aliyunCLIForwardEnv": [],
            "sessionId": None,
            "regionId": "cn-hangzhou",
            "profile": None,
            "noThinking": False,
            "connectTimeout": 10,
            "readTimeout": 600,
            "aliyunPath": "aliyun",
            "clientContext": None,
            "attachments": [],
        },
    )
    payload = _status_event(state="TASK_STATE_WORKING", text="must not be projected")
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda _command, **_kwargs: _FakeCLIProcess(json.dumps({"data": payload})),
    )
    stopped = []

    def stop_chat(_job, session_id):
        stopped.append(session_id)
        return {"status": "Stopped", "sessionId": session_id}

    monkeypatch.setattr(bridge, "_run_stop_chat", stop_chat)

    assert bridge.run_worker(job_id, request_token) == 0

    job = bridge._load_state_json(job_path)
    assert stopped == ["session-1"]
    assert job["sessionId"] == "session-1"
    assert job["state"] == "canceled"
    assert "streamCursor" not in job
    assert bridge._read_spool(spool) == []


def test_remote_bootstrap_session_mismatch_fails_before_projection_or_ack(monkeypatch, tmp_path: Path) -> None:
    payload = _status_event(state="TASK_STATE_WORKING")
    payload["result"]["statusUpdate"]["contextId"] = "session-from-payload"
    ack_writes = []

    def popen(_command, **kwargs):
        invocation = kwargs["env"][bridge.REMOTE_BOOTSTRAP_INVOCATION_ENV]
        output = json.dumps(
            {
                "invocationId": invocation,
                "event": {"id": "v1.session-mismatch.1", "data": payload},
                "sessionId": "session-from-bootstrap",
            }
        )
        return _FakeCLIProcess(output)

    original_atomic_json = bridge._atomic_json

    def atomic_json(path, value, mode=0o600):
        if Path(path).name == "bootstrap-ack.json":
            ack_writes.append(dict(value))
        return original_atomic_json(path, value, mode)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/remote/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge, "_atomic_json", atomic_json)

    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(
            _chat_args(transport="aliyun_cli", aliyun_cli_execution_mode="remote"),
            tmp_path,
            "hello",
            None,
            [],
        )

    assert error.value.code == "stream_session_mismatch"
    assert ack_writes == []


@pytest.mark.parametrize("failure", ["exception", "failed"])
def test_stop_chat_failure_is_persisted_as_failed(monkeypatch, tmp_path: Path, failure: str) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "3" * 32
    root, job_path, spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    spool.touch()
    bridge._atomic_json(
        job_path,
        {
            "schemaVersion": 1,
            "jobId": job_id,
            "state": "working",
            "mode": "normal",
            "sessionId": "session-1",
            "activeRequestSeq": 1,
            "turn": 1,
            "artifacts": [],
        },
    )

    def stop_chat(_job, _session_id):
        if failure == "exception":
            raise bridge.BridgeError("stop_chat_failed", "timeout", True)
        return {"status": "Failed", "sessionId": "session-1"}

    monkeypatch.setattr(bridge, "_run_stop_chat", stop_chat)

    result = bridge._cancel_job_local({"jobId": job_id})

    assert result["state"] == "cancel-failed"
    job = bridge._load_state_json(job_path)
    assert job["state"] == "failed"
    assert job["stopStatus"] == "Failed"
    assert job["error"]["code"] == "stop_chat_failed"
    assert job["error"]["retryable"] is True


@pytest.mark.parametrize(
    ("executor_version", "executor_capabilities"),
    [(None, None), ("", ""), ("executor-1.2.3", "other"), ("executor-1.2.3", "startchat-reconnect-bootstrap-v1")],
)
def test_remote_check_does_not_require_executor_metadata(monkeypatch, executor_version, executor_capabilities) -> None:
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/remote/aliyun")
    for name, value in (
        ("ALICLOUD_ROS_AGENT_EXECUTOR_VERSION", executor_version),
        ("ALICLOUD_ROS_AGENT_EXECUTOR_CAPABILITIES", executor_capabilities),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(
        args,
        {
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
            "endpoint": "ros-pre.aliyuncs.com",
        },
    )

    result = bridge.run_check(args)

    assert result["startChatReconnectReady"] is True
    assert "remoteExecutorVersion" not in result
    assert "remoteExecutorCapabilities" not in result
    assert "startChatReconnectBlockers" not in result


def test_remote_check_still_requires_an_available_executor(monkeypatch) -> None:
    monkeypatch.setattr(bridge.shutil, "which", lambda _path: None)
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, {"transport": "aliyun_cli", "aliyunCLIExecutionMode": "remote"})

    with pytest.raises(bridge.BridgeError) as error:
        bridge.run_check(args)

    assert error.value.code == "cli_not_found"


def test_cli_size_limits_are_distinct_and_enforced_after_wrapper_decode(monkeypatch) -> None:
    assert bridge.MAX_CLI_BATCH_BYTES > bridge.MAX_SERVER_REPLAY_BYTES > bridge.MAX_SSE_EVENT_BYTES
    monkeypatch.setattr(bridge, "MAX_SSE_EVENT_BYTES", 100)
    monkeypatch.setattr(bridge, "MAX_SERVER_REPLAY_BYTES", 150)
    monkeypatch.setattr(bridge, "MAX_CLI_BATCH_BYTES", 400)
    first = {"contextId": "session-1", "value": "x" * 45}
    second = {"contextId": "session-1", "value": "y" * 45}
    wrapped = json.dumps([{"id": "v1.size.1", "data": first}, {"id": "v1.size.2", "data": second}])

    with pytest.raises(bridge.BridgeError) as error:
        list(bridge.iter_cli_plugin_payloads([wrapped]))
    assert error.value.code == "server_replay_too_large"
    assert error.value.retryable is False

    monkeypatch.setattr(bridge, "MAX_CLI_BATCH_BYTES", 20)
    with pytest.raises(bridge.BridgeError) as error:
        list(bridge.iter_cli_plugin_payloads([wrapped]))
    assert error.value.code == "cli_output_too_large"

    monkeypatch.setattr(bridge, "MAX_CLI_BATCH_BYTES", 100)
    small = json.dumps({"id": "v1.size.1", "data": {"value": "x" * 24}}) + "\n"
    assert len(small.encode("utf-8")) < bridge.MAX_CLI_BATCH_BYTES
    with pytest.raises(bridge.BridgeError) as error:
        list(bridge.iter_cli_plugin_payloads([small, small]))
    assert error.value.code == "cli_output_too_large"


def test_bootstrap_copy_does_not_double_count_server_replay_budget(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "MAX_SSE_EVENT_BYTES", 1000)
    monkeypatch.setattr(bridge, "MAX_SERVER_REPLAY_BYTES", 180)
    monkeypatch.setattr(bridge, "MAX_CLI_BATCH_BYTES", 2000)
    payload = {"contextId": "session-1", "value": "x" * 80}
    bootstrap = {
        "invocationId": "invocation-1",
        "event": {"id": "v1.bootstrap-size.1", "data": payload},
        "sessionId": "session-1",
    }
    normal = [
        {"id": "v1.bootstrap-size.1", "data": payload},
        {"id": "v1.bootstrap-size.2", "data": {"contextId": "session-1", "value": "tail"}},
    ]

    events = list(bridge.iter_cli_plugin_payloads([json.dumps(bootstrap) + "\n", json.dumps(normal)]))

    assert [event["id"] for event in events] == [
        "v1.bootstrap-size.1",
        "v1.bootstrap-size.1",
        "v1.bootstrap-size.2",
    ]


@pytest.mark.parametrize("transport", ["code", "aliyun_cli"])
def test_business_event_without_id_fails_before_projection(monkeypatch, tmp_path: Path, transport: str) -> None:
    payload = _status_event(state="TASK_STATE_WORKING")
    projected = []
    args = _chat_args(transport=transport, session_id=None)
    if transport == "code":

        class Response:
            headers = {"Content-Type": "text/event-stream"}

            def __iter__(self):
                return iter([("data: " + json.dumps(payload) + "\n").encode(), b"\n"])

            def close(self):
                return None

        monkeypatch.setattr(bridge, "_open_code_request", lambda *_args, **_kwargs: Response())
    else:
        monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
        monkeypatch.setattr(
            bridge.subprocess,
            "Popen",
            lambda _command, **_kwargs: _FakeCLIProcess(json.dumps({"data": payload})),
        )

    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(
            args,
            tmp_path,
            "hello",
            None,
            [],
            on_payload=lambda *_args: projected.append(True),
        )

    assert error.value.code == "missing_stream_event_id"
    assert projected == []


def test_stream_identity_change_fails_without_reconnect(monkeypatch, tmp_path: Path) -> None:
    payload = _status_event(state="TASK_STATE_WORKING")
    output = json.dumps(
        [
            {"id": "v1.first.1", "data": payload},
            {"id": "v1.second.2", "data": payload},
        ]
    )
    calls = []
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda command, **_kwargs: calls.append(command) or _FakeCLIProcess(output),
    )

    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(_chat_args(transport="aliyun_cli"), tmp_path, "hello", None, [])

    assert error.value.code == "stream_event_identity_mismatch"
    assert len(calls) == 1


def test_primary_permission_ack_reconnects_but_sideband_ack_is_an_immediate_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    response = {"inputId": "input-1", "toolUseId": "tool-1", "decision": "allow_once"}
    acknowledgement = {
        "result": {
            "message": {
                "contextId": "session-1",
                "parts": [
                    {
                        "mediaType": "application/json",
                        "data": {
                            "schemaVersion": 1,
                            "kind": "permission_ack",
                            "inputId": "input-1",
                            "toolUseId": "tool-1",
                            "decision": "allow_once",
                            "accepted": True,
                        },
                    }
                ],
            }
        }
    }
    completed = _status_event(state="TASK_STATE_COMPLETED")

    def run(role):
        attempts = [
            json.dumps({"id": "v1.permission.1", "data": acknowledgement}),
            json.dumps({"id": "v1.permission.2", "data": completed}),
        ]
        commands = []

        def popen(command, **_kwargs):
            commands.append(command)
            return _FakeCLIProcess(attempts.pop(0))

        monkeypatch.setattr(bridge.subprocess, "Popen", popen)
        result = bridge._consume_start_chat(
            _chat_args(transport="aliyun_cli", session_id="session-1"),
            tmp_path,
            "permission",
            None,
            [],
            worker_role=role,
            permission_response=response,
        )
        return result, commands

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    primary_result, primary_commands = run("primary")
    sideband_result, sideband_commands = run("sideband")

    assert primary_result["state"] == "turn-completed"
    assert len(primary_commands) == 2
    assert "--stream-options" not in primary_commands[1]
    assert _command_body(primary_commands[1]) == {
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.permission.1",
    }
    assert sideband_result["state"] == "permission-responded"
    assert len(sideband_commands) == 1


def test_retryable_initial_failure_does_not_resend_the_business_query(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def open_request(_operation, parameters, *_args, **_kwargs):
        calls.append(parameters)
        raise bridge.BridgeError("start_chat_failed", "throttled", True)

    monkeypatch.setattr(bridge, "_open_code_request", open_request)

    with pytest.raises(bridge.BridgeError) as error:
        bridge._consume_start_chat(_chat_args(transport="code"), tmp_path, "hello", None, [])

    assert error.value.code == "missing_recovery_anchor"
    assert len(calls) == 1
    assert calls[0]["Query"] == "hello"


def test_unknown_cli_nonzero_exit_is_not_retried(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        kwargs["stderr"].write(b"unknown local failure")
        return _FakeCLIProcess("", 2)

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)

    result = bridge._consume_start_chat(_chat_args(transport="aliyun_cli"), tmp_path, "hello", None, [])

    assert result["state"] == "failed"
    assert result["error"]["code"] == "aliyun_cli_failed"
    assert len(calls) == 1


def test_structured_retryable_cli_error_on_stdout_reconnects_from_anchor(monkeypatch, tmp_path: Path) -> None:
    working = _status_event(state="TASK_STATE_WORKING", text="working")
    completed = _status_event(
        state="TASK_STATE_COMPLETED", text="done", metadata={"assistantFinal": {"complete": True}}
    )
    calls = []

    def popen(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            output = "\n".join(
                [
                    json.dumps({"id": "v1.stdout-error.1", "data": working}),
                    json.dumps({"statusCode": 503, "code": "ServiceUnavailable"}),
                ]
            )
            return _FakeCLIProcess(output, 1)
        return _FakeCLIProcess(json.dumps({"id": "v1.stdout-error.2", "data": completed}))

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)

    result = bridge._consume_start_chat(_chat_args(transport="aliyun_cli"), tmp_path, "hello", None, [])

    assert result["state"] == "turn-completed"
    assert len(calls) == 2
    assert _command_body(calls[1]) == {
        "StreamOptions.Action": "Reconnect",
        "StreamOptions.Cursor": "v1.stdout-error.1",
    }


def test_same_worker_apply_checkpoint_gap_does_not_duplicate_summary(monkeypatch, tmp_path: Path) -> None:
    first = _status_event(state="TASK_STATE_WORKING", text="a")
    second = _status_event(state="TASK_STATE_WORKING", text="b")
    completed = _status_event(
        state="TASK_STATE_COMPLETED", text="done", metadata={"assistantFinal": {"complete": True}}
    )
    attempts = [
        json.dumps(
            [
                {"id": "v1.gap.1", "data": first},
                {"id": "v1.gap.2", "data": second},
            ]
        ),
        json.dumps(
            [
                {"id": "v1.gap.2", "data": second},
                {"id": "v1.gap.3", "data": completed},
            ]
        ),
    ]
    callback_calls = []

    def callback(event_id, _payload, _summary, already_applied):
        callback_calls.append((event_id, already_applied))
        if event_id == "v1.gap.2" and not already_applied:
            raise bridge.BridgeError("stream_failed", "checkpoint write interrupted", True)
        return True

    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda _command, **_kwargs: _FakeCLIProcess(attempts.pop(0)),
    )
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)

    result = bridge._consume_start_chat(
        _chat_args(transport="aliyun_cli"), tmp_path, "hello", None, [], on_payload=callback
    )

    assert result["eventCount"] == 3
    assert callback_calls == [
        ("v1.gap.1", False),
        ("v1.gap.2", False),
        ("v1.gap.2", True),
        ("v1.gap.3", False),
    ]


@pytest.fixture
def remote_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    live_context = tmp_path / "live-context"
    live_context.write_text("fake-context-one", encoding="utf-8")
    monkeypatch.setenv("STAROPS_HIL", "fake-context-one")
    calls = tmp_path / "calls.jsonl"
    cli = _write_fake_aliyun(tmp_path, """import json, os, sys, time
from pathlib import Path
calls = Path(%r)
context = Path(%r)
pipeline = context.with_suffix('.pipeline')
multiwindow = context.with_suffix('.multiwindow')
acknowledged = context.with_suffix('.acknowledged')
finish = context.with_suffix('.finish')
disconnect = context.with_suffix('.disconnect')
disconnect_again = context.with_suffix('.disconnect-again')
def wait_for(*paths):
    timeout_file = context.with_suffix('.timeout')
    deadline = time.monotonic() + (float(timeout_file.read_text(encoding='utf-8')) if timeout_file.exists() else 20)
    while not any(path.exists() for path in paths):
        if time.monotonic() > deadline:
            raise SystemExit(2)
        time.sleep(0.02)
args = sys.argv[1:]
with calls.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps({'args': args, 'hil': os.environ.get('STAROPS_HIL'),
                             'pid': os.getpid(), 'parent': os.getppid()}) + '\\n')
if os.environ.get('STAROPS_HIL') != context.read_text(encoding='utf-8'):
    print(json.dumps({'summary': 'Failed to call tool', 'popRequestId': 'fake-request',
                      'detail': 'HIL context expired ' + os.environ.get('STAROPS_HIL', '')}), flush=True)
    raise SystemExit(1)
if args[:2] == ['ros', 'stop-chat']:
    print(json.dumps({'Status': 'Stopped', 'SessionId': 'session-1'}), flush=True)
    raise SystemExit(0)
if args[:2] == ['ros', 'list-chat-sessions']:
    assert args[args.index('--agent-version') + 1] == 'V2'
    assert args[args.index('--biz-mode') + 1] in ('IaCCodeNormal', 'IaCCodePipeline')
    assert args[args.index('--max-results') + 1] == '20'
    assert json.loads(args[args.index('--body') + 1]) == {'SessionScopeOption': 'CurrentCaller'}
    response = context.with_suffix('.session-list.json')
    print(response.read_text(encoding='utf-8') if response.exists() else json.dumps({'Sessions': []}), flush=True)
    raise SystemExit(0)
query = args[args.index('--query') + 1] if '--query' in args else None
if query and query.startswith('IAC_CODE_PERMISSION:') and context.with_suffix('.permission-loss').exists():
    context.with_suffix('.permission-loss').unlink()
    context.with_suffix('.permission-response').write_text(query.split(':', 1)[1], encoding='utf-8')
    print('Aliyun CLI call failed: failed to call tool: SSE connection lost', file=sys.stderr, flush=True)
    raise SystemExit(1)
body = json.loads(args[args.index('--body') + 1]) if '--body' in args else {}
if query is None and body.get('StreamOptions.Cursor') == 'v1.permission.0':
    response = json.loads(context.with_suffix('.permission-response').read_text(encoding='utf-8'))
    ack = dict(response, kind='permission_ack', accepted=True)
    event = {'result': {'message': {'contextId': 'session-1', 'parts': [
        {'mediaType': 'application/json', 'data': ack}]}}}
    print(json.dumps({'id': 'v1.permission.1', 'data': event}), flush=True)
    terminal = {'result': {'statusUpdate': {'contextId': 'session-1', 'taskId': 'task-1',
        'status': {'state': 'TASK_STATE_COMPLETED', 'message': {'role': 'ROLE_AGENT', 'parts': [
            {'text': 'authoritative final'}]}},
        'metadata': {'iac_code': {'assistantFinal': {'complete': True}}}}}}
    print(json.dumps({'id': 'v1.permission.2', 'data': terminal}), flush=True)
    raise SystemExit(0)
if query is None and body.get('StreamOptions.Cursor') == 'v1.lifecycle.0':
    print(json.dumps({'id': 'v1.lifecycle.1', 'data': {'result': {'statusUpdate': {
        'taskId': 'task-1', 'contextId': 'session-1', 'status': {'state': 'TASK_STATE_WORKING'}}}}}), flush=True)
if query in ('sse-lost', 'sse-lost-no-anchor'):
    if query == 'sse-lost':
        print(json.dumps({'id': 'v1.lifecycle.1', 'data': {'result': {'statusUpdate': {
            'taskId': 'task-1', 'contextId': 'session-1', 'status': {'state': 'TASK_STATE_WORKING'}}}}}), flush=True)
    print('Aliyun CLI call failed: failed to call tool: SSE connection lost', file=sys.stderr, flush=True)
    raise SystemExit(1)
if query and query.startswith('input-'):
    kind = query[len('input-'):].split('-truncated', 1)[0]
    envelope = {'schemaVersion': 1, 'kind': kind, 'requestTaskId': 'task-1', 'contextId': 'session-1',
                'inputId': 'input-1', 'prompt': 'Choose A', 'options': [{'id': 'A', 'label': 'A'}]}
    if kind == 'permission':
        envelope['toolUseId'] = 'tool-1'
    event = {'id': 'v1.input.1', 'data': {'result': {'statusUpdate': {
        'taskId': 'task-1', 'contextId': 'session-1', 'status': {'state': 'TASK_STATE_INPUT_REQUIRED'},
        'metadata': {'iac_code': {'input': envelope}}}}}}
    if query.endswith('-truncated'):
        print('[' + json.dumps(event) + ', {"id":"v1.input.2","data":', end='', flush=True)
        raise SystemExit(0)
    else:
        print(json.dumps(event), flush=True)
    wait_for(finish, disconnect)
elif query == 'receipt-then-input':
    receipt = {'eventType': 'input_received', 'status': 'working', 'data': {
        'kind': 'ask_user_question', 'inputId': 'input-1',
        'options': [{'id': 'A', 'label': 'A'}], 'allowFreeText': True}}
    envelope = {'schemaVersion': 1, 'kind': 'ask_user_question', 'requestTaskId': 'task-1',
                'contextId': 'session-1', 'inputId': 'input-2', 'prompt': 'Development or production?',
                'options': [{'id': 'dev', 'label': 'Development'}, {'id': 'prod', 'label': 'Production'}]}
    for number, state, metadata in (
        (1, 'TASK_STATE_WORKING', {'pipeline': receipt}),
        (2, 'TASK_STATE_INPUT_REQUIRED', {'input': envelope}),
    ):
        print(json.dumps({'id': 'v1.reply.' + str(number), 'data': {'result': {'statusUpdate': {
            'taskId': 'task-1', 'contextId': 'session-1', 'status': {'state': state},
            'metadata': {'iac_code': metadata}}}}}), flush=True)
    wait_for(finish, disconnect)
elif query == 'truncated-array':
    first = {'id': 'v1.lifecycle.1', 'data': {'result': {'statusUpdate': {
        'taskId': 'task-1', 'contextId': 'session-1', 'status': {'state': 'TASK_STATE_WORKING'}}}}}
    incomplete = {'id': 'v1.lifecycle.99', 'data': {'contextId': 'wrong-session', 'padding': 'x' * 90000}}
    print(json.dumps([first, incomplete], indent=2)[:80 * 1024], end='', flush=True)
    wait_for(disconnect, finish)
elif query in ('no-anchor', 'delayed-approval'):
    queued_at = time.monotonic()
    wait_for(context.with_suffix('.approval') if query == 'delayed-approval' else finish, disconnect)
    if disconnect.exists():
        raise SystemExit(0)
    if query == 'delayed-approval':
        foreground_pid = int(context.with_suffix('.foreground').read_text(encoding='utf-8'))
        # Simulate admission tied to the original foreground tool invocation,
        # not merely to the detached CLI reader's parent process.
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x00100000, False, foreground_pid)
            if not handle:
                raise SystemExit('Foreground expired before approval')
            try:
                if kernel32.WaitForSingleObject(handle, 0) != 0x00000102:
                    raise SystemExit('Foreground expired before approval')
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(foreground_pid, 0)
        if os.environ.get('STAROPS_HIL') != context.read_text(encoding='utf-8'):
            raise SystemExit('HIL expired before approval')
        context.with_suffix('.admitted').write_text(json.dumps({
            'foregroundPid': foreground_pid, 'foregroundAlive': True,
            'waitedSeconds': time.monotonic() - queued_at,
        }), encoding='utf-8')
    print(json.dumps([{'id': 'v1.buffered.1', 'data': json.loads(%r)}]), flush=True)
elif query in ('first', 'pipeline-first', 'multi-window-first', 'two-deltas'):
    if query == 'pipeline-first':
        pipeline.touch()
    if query == 'multi-window-first':
        multiwindow.touch()
    first = json.loads(%r)
    print(json.dumps({'id': 'v1.lifecycle.1', 'data': first}), flush=True)
    if query == 'two-deltas':
        first['result']['statusUpdate']['status']['message']['parts'][0]['text'] = 'second partial'
        print(json.dumps({'id': 'v1.lifecycle.2', 'data': first}), flush=True)
    wait_for(finish, disconnect, acknowledged)
    if disconnect.exists():
        raise SystemExit(0)
    print(json.dumps({'id': 'v1.lifecycle.3' if query == 'two-deltas' else 'v1.lifecycle.2',
                      'data': json.loads(%r)}), flush=True)
elif query is None and multiwindow.exists() and 'v1.lifecycle.1' in args[args.index('--body') + 1]:
    print(json.dumps({'id': 'v1.lifecycle.2', 'data': json.loads(%r)}), flush=True)
    wait_for(disconnect_again, finish)
    raise SystemExit(0)
elif query and query.startswith('IAC_CODE_PERMISSION:'):
    response = json.loads(query.split(':', 1)[1])
    ack = dict(response, kind='permission_ack', accepted=True)
    event = {'result': {'message': {'contextId': 'session-1', 'parts': [
        {'mediaType': 'application/json', 'data': ack}]}}}
    print(json.dumps({'id': 'v1.permission.1', 'data': event}), flush=True)
    if pipeline.exists():
        acknowledged.touch()
    print(json.dumps({'id': 'v1.permission.2', 'data': json.loads(%r)}), flush=True)
elif query is None and pipeline.exists():
    while not acknowledged.exists():
        time.sleep(0.02)
    print(json.dumps({'id': 'v1.lifecycle.2', 'data': json.loads(%r)}), flush=True)
else:
    print(json.dumps({'id': ('v1.lifecycle.3' if multiwindow.exists() else 'v1.lifecycle.2')
                      if query is None else 'v1.next-turn.1',
                      'data': json.loads(%r)}), flush=True)
""" % (
        str(calls), str(live_context),
        json.dumps(_status_event(state="TASK_STATE_COMPLETED", text="buffered final",
                                 metadata={"assistantFinal": {"complete": True}})),
        json.dumps(_status_event(text="first partial")),
        json.dumps(_status_event(state="TASK_STATE_COMPLETED", text="authoritative final",
                                 metadata={"assistantFinal": {"complete": True}})),
        json.dumps(_status_event(text="second partial")),
        json.dumps(_status_event(state="TASK_STATE_COMPLETED")),
        json.dumps(_status_event(state="TASK_STATE_COMPLETED")),
        json.dumps(_status_event(state="TASK_STATE_COMPLETED", text="authoritative final",
                                 metadata={"assistantFinal": {"complete": True}})),
    ))

    def start(prompt="first", mode="normal", aliyun_path=None):
        started = bridge._start_job_local({
            "workspace": str(workspace), "prompt": prompt, "mode": mode, "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote", "aliyunCLIForwardEnv": ["STAROPS_HIL"],
            "transientEnvironment": {"STAROPS_HIL": "fake-context-one"},
            "endpoint": "ros.aliyuncs.com", "aliyunPath": str(cli) if aliyun_path is None else aliyun_path,
            "debug": True,
        })
        root = bridge._remote_cli_root(started["jobId"])
        deadline = time.monotonic() + 5
        while not list(root.glob("remote-cli-*.request.json")):
            assert time.monotonic() < deadline, "The parser worker did not enqueue its CLI invocation."
            time.sleep(0.02)
        return started

    def refresh(value="fake-context-two"):
        live_context.write_text(value, encoding="utf-8")
        monkeypatch.setenv("STAROPS_HIL", value)

    def read_calls():
        return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []

    def disconnect(job_id, again=False):
        live_context.with_suffix(".disconnect-again" if again else ".disconnect").touch()
        root = bridge._remote_cli_root(job_id)
        deadline = time.monotonic() + 5
        while True:
            # The parser retires old mailboxes under this same lock.
            with bridge.StateLock(root / ".remote-cli.lock"):
                reconnect_queued = any(
                    bridge._load_state_json(path).get("ownerPid") is None
                    for path in root.glob("remote-cli-*.request.json")
                )
            if reconnect_queued:
                break
            assert time.monotonic() < deadline, "The parser did not queue a reconnect after disconnection."
            time.sleep(0.02)

    foregrounds = []

    def drive(job_id, cursor=0, seconds=1):
        process = subprocess.Popen([
            sys.executable, "-c",
            "import runpy,sys; b=runpy.run_path(sys.argv[1]); "
            "b['_drive_foreground_cli'](sys.argv[2],int(sys.argv[3]),float(sys.argv[4]))",
            str(BRIDGE_PATH), job_id, str(cursor), str(seconds),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        foregrounds.append(process)
        live_context.with_suffix('.foreground').write_text(str(process.pid), encoding='utf-8')
        return process

    def start_command(explicit_follow):
        scripts = tmp_path / 'installed-skill' / 'scripts'
        scripts.mkdir(parents=True)
        for source in BRIDGE_SOURCE_PATHS:
            shutil.copyfile(source, scripts / source.name)
        (scripts.parent / 'config.json').write_text(json.dumps({
            'transport': 'aliyun_cli', 'aliyunCLIExecutionMode': 'remote', 'allowedAgentModes': ['normal'],
            'managerIdleSeconds': 60, 'enableThinking': True, 'aliyunCLIForwardEnv': ['STAROPS_HIL'], 'debug': True,
        }), encoding='utf-8')
        prompt = workspace / 'prompt.md'
        prompt.write_text('no-anchor', encoding='utf-8')
        command = [
            sys.executable, str(scripts / BRIDGE_PATH.name), 'start', '--prompt-file', str(prompt),
            '--aliyun-path', str(cli), '--follow-seconds', '1',
        ]
        if explicit_follow:
            command.append('--follow')
        process = subprocess.Popen(command, cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        foregrounds.append(process)
        return process

    def wait_for(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, "Timed out waiting for the remote lifecycle checkpoint."
            time.sleep(0.02)

    def finish_drive(process, timeout=8):
        output, errors = process.communicate(timeout=timeout)
        assert process.returncode == 0, (output, errors)

    yield SimpleNamespace(start=start, refresh=refresh, calls=read_calls, workspace=workspace,
                          context=live_context, disconnect=disconnect, drive=drive,
                          wait_for=wait_for, finish_drive=finish_drive, start_command=start_command)
    live_context.with_suffix(".finish").touch()
    for process in foregrounds:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
    # Failed assertions must not leave a detached fake parser parked forever.
    # Set local stop intent only; teardown never invokes a cloud operation.
    for job_path in (tmp_path / 'state' / 'jobs').glob('*/job.json'):
        with bridge.StateLock(job_path.parent / '.job.lock'):
            job = bridge._load_state_json(job_path)
            workers = [job.get(key) for key in ('workerPid', 'sidebandWorkerPid')
                       if bridge._pid_alive(job.get(key))]
            if workers:
                job.setdefault('stopRequestedAt', int(time.time()))
                bridge._atomic_json(job_path, job)
        for pid in workers:
            _wait_for_pid_exit(pid)
            assert not bridge._pid_alive(pid), 'A fake parser leaked from the lifecycle fixture.'


def test_remote_cli_default_command_name_resolves_before_foreground_handoff(remote_lifecycle, monkeypatch):
    env = remote_lifecycle
    monkeypatch.setenv("PATH", str(env.context.parent) + os.pathsep + os.environ.get("PATH", ""))
    started = env.start(aliyun_path="aliyun")
    job_id = started["jobId"]
    job = bridge._load_state_json(bridge._job_paths(job_id)[1])
    assert Path(job["aliyunPath"]) == env.context.parent / ("aliyun.cmd" if os.name == "nt" else "aliyun")
    env.context.with_suffix(".finish").touch()
    bridge._drive_foreground_cli(job_id, 0, 5)
    final = bridge._follow_job_local(job_id, 0, 1)
    assert final["state"] == "turn-completed"
    assert len(env.calls()) == 1


def test_remote_busy_foreground_stays_alive_without_follow_and_next_turn_uses_fresh_hil(remote_lifecycle):
    env = remote_lifecycle
    started = env.start()
    job_id = started["jobId"]
    time.sleep(0.15)
    assert env.calls() == []  # A detached parser worker cannot invoke remote aliyun.
    foreground = env.drive(job_id, seconds=0.05)
    env.wait_for(lambda: len(env.calls()) == 1)
    time.sleep(0.3)  # Well beyond the scaled-down observation timeout.
    assert foreground.poll() is None
    first = bridge._follow_job_local(job_id, 0, 0)
    assert first["followTimedOut"] is True
    assert env.calls()[0]["hil"] == "fake-context-one"
    if os.name != "nt":
        assert bridge._pid_alive(env.calls()[0]["pid"])

    env.context.with_suffix(".finish").touch()
    env.finish_drive(foreground)
    final = bridge._follow_job_local(job_id, first["cursor"], 5)
    assert final["state"] == "turn-completed"
    assert final["finalText"] == "authoritative final"
    assert len(env.calls()) == 1
    assert "--query" in env.calls()[0]["args"]
    root, _job_path, spool = bridge._job_paths(job_id)
    assert [item.get("streamEventId") for item in bridge._read_spool(spool)].count("v1.lifecycle.1") == 1
    assert not list(bridge._remote_cli_root(job_id).glob("remote-cli-*"))
    for path in root.iterdir():
        assert "fake-context-" not in path.read_text(encoding="utf-8", errors="replace")

    env.refresh("fake-context-three")
    continued = bridge._continue_job_local({"jobId": job_id, "prompt": "next question"})
    bridge._drive_foreground_cli(job_id, continued["cursor"], 5)
    next_turn = bridge._follow_job_local(job_id, continued["cursor"], 1)
    assert next_turn["state"] == "turn-completed"
    assert next_turn["turn"] == 2
    assert env.calls()[-1]["hil"] == "fake-context-three"
    assert "--query" in env.calls()[-1]["args"]
    assert "--session-id" in env.calls()[-1]["args"]


@pytest.mark.parametrize("blocked", ["launcher-gone", "launch-timed-out"])
def test_remote_reader_cannot_launch_after_foreground_handoff_fails(remote_lifecycle, monkeypatch, blocked):
    env = remote_lifecycle
    started = env.start()
    job_id = started["jobId"]
    root = bridge._remote_cli_root(job_id)
    path = next(root.glob("remote-cli-*.request.json"))
    if blocked == "launch-timed-out":
        with monkeypatch.context() as patch:
            patch.setattr(bridge, "MANAGER_START_TIMEOUT_SECONDS", 0.05)
            patch.setattr(bridge.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: None))
            with pytest.raises(bridge.BridgeError, match="did not start in time"):
                bridge._drive_foreground_cli(job_id, 0, 1)
        assert path.with_suffix(".stop").exists()
    else:
        request = bridge._load_state_json(path)
        request["ownerPid"] = os.getpid()
        bridge._atomic_json(path, request)
        monkeypatch.setattr(bridge, "_pid_alive", lambda _pid: False)
    invocation_id = path.name.split(".")[0][len("remote-cli-"):]
    assert bridge.run_remote_cli(job_id, invocation_id) == 1
    assert env.calls() == []


def test_remote_delayed_approval_or_buffered_output_does_not_end_foreground_without_anchor(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("no-anchor")
    foreground = env.drive(started["jobId"], seconds=0.05)
    env.wait_for(lambda: len(env.calls()) == 1)
    time.sleep(0.3)
    assert foreground.poll() is None
    result = bridge._follow_job_local(started["jobId"], 0, 0)
    assert result["followTimedOut"] is True
    assert result["state"] != "failed"
    assert len(env.calls()) == 1
    env.context.with_suffix(".finish").touch()
    env.finish_drive(foreground)
    result = bridge._follow_job_local(started["jobId"], result["cursor"], 5)
    assert result["state"] == "turn-completed", result
    assert result["finalText"] == "buffered final"
    assert len(env.calls()) == 1


def test_remote_approval_gate_checks_original_foreground_after_observation_window(remote_lifecycle, record_property):
    env = remote_lifecycle
    # CI uses the same ordering at a scaled timeout; a targeted local run can
    # exercise the real 60-second window with approval after 65 seconds.
    delay = float(os.environ.get('ROS_AGENT_TEST_APPROVAL_DELAY_SECONDS', '0.3'))
    window = float(os.environ.get('ROS_AGENT_TEST_FOREGROUND_IDLE_SECONDS', '0.05'))
    assert delay > window
    env.context.with_suffix('.timeout').write_text(str(delay + 20), encoding='utf-8')
    started = env.start('delayed-approval')
    foreground = env.drive(started['jobId'], seconds=window)
    env.wait_for(lambda: len(env.calls()) == 1)
    time.sleep(delay)
    # No follow/heartbeat request keeps the foreground alive during this wait.
    assert foreground.poll() is None
    job = bridge._load_state_json(bridge._job_paths(started['jobId'])[1])
    assert job.get('sessionId') is None
    assert job.get('streamCursor') is None
    assert not env.context.with_suffix('.admitted').exists()
    env.context.with_suffix('.approval').touch()
    env.finish_drive(foreground)
    admitted = json.loads(env.context.with_suffix('.admitted').read_text(encoding='utf-8'))
    assert admitted['foregroundPid'] == foreground.pid
    assert admitted['foregroundAlive'] is True
    assert admitted['waitedSeconds'] > window
    final = bridge._follow_job_local(started['jobId'], 0, 5)
    assert final['state'] == 'turn-completed', final
    assert final['finalText'] == 'buffered final'
    assert len(env.calls()) == 1
    record_property('approval_delay_seconds', round(admitted['waitedSeconds'], 3))
    record_property('foreground_idle_seconds', window)


def test_remote_managed_reader_flushes_after_five_seconds_of_real_output_silence(remote_lifecycle, record_property):
    env = remote_lifecycle
    started = env.start('two-deltas')
    job_id = started['jobId']
    foreground = env.drive(job_id)
    job_path = bridge._job_paths(job_id)[1]
    env.wait_for(lambda: bridge._load_state_json(job_path).get('streamCursor') == 'v1.lifecycle.1')
    observed_at = time.monotonic()
    # The second delta is below the count/byte limits. No third event arrives
    # to trigger a flush; the real mailbox reader's idle hook must do it.
    env.wait_for(lambda: bridge._load_state_json(job_path).get('streamCursor') == 'v1.lifecycle.2', timeout=8)
    elapsed = time.monotonic() - observed_at
    assert 3 < elapsed < 8
    assert foreground.poll() is None
    assert len(env.calls()) == 1
    env.context.with_suffix('.finish').touch()
    env.finish_drive(foreground)
    final = bridge._follow_job_local(job_id, 0, 5)
    assert final['state'] == 'turn-completed', final
    assert bridge._load_state_json(job_path)['streamCursor'] == 'v1.lifecycle.3'
    assert len(env.calls()) == 1
    record_property('silent_flush_observed_seconds', round(elapsed, 3))


@pytest.mark.parametrize('explicit_follow', [False, True])
def test_remote_public_start_command_finishes_without_a_second_follow_command(remote_lifecycle, explicit_follow):
    env = remote_lifecycle
    foreground = env.start_command(explicit_follow)
    env.wait_for(lambda: len(env.calls()) == 1 or foreground.poll() is not None)
    if foreground.poll() is not None:
        pytest.fail('Start exited before CLI admission: {!r}'.format(foreground.communicate(timeout=1)))
    manager = bridge._load_state_json(bridge._manager_record_path())
    # Shorten idle expiry only after CLI admission. A loaded Windows caller
    # can otherwise exceed one second between startup health and /start.
    # Keep the original busy-work and post-completion shutdown assertions.
    manager['idleSeconds'] = 1
    bridge._atomic_json(bridge._manager_record_path(), manager)
    time.sleep(1.3)  # Exceeds both configured manager idle and the foreground observation window.
    assert foreground.poll() is None
    assert bridge._pid_alive(manager['pid'])
    assert len(env.calls()) == 1
    env.context.with_suffix('.finish').touch()
    output, errors = foreground.communicate(timeout=8)
    assert foreground.returncode == 0, (output, errors)
    final = json.loads(output.decode('utf-8'))
    assert final['state'] == 'turn-completed', final
    assert final['finalText'] == 'buffered final'
    assert len(output) < 32 * 1024
    assert len(env.calls()) == 1
    _wait_for_pid_exit(final['workerPid'])
    assert not bridge._pid_alive(final['workerPid'])
    _wait_for_pid_exit(manager['pid'])
    assert not bridge._pid_alive(manager['pid'])


def test_remote_real_disconnect_before_anchor_fails_without_retrying_query(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("no-anchor")
    # This case includes bounded session-list probes. The idle window must
    # cover their parser handoffs, not just the initial StartChat invocation.
    foreground = env.drive(started["jobId"], seconds=5)
    env.wait_for(lambda: len(env.calls()) == 1)
    env.context.with_suffix(".disconnect").touch()
    env.finish_drive(foreground, timeout=20)
    result = bridge._follow_job_local(started["jobId"], 0, 5)
    assert result["state"] == "failed"
    assert result["error"]["code"] == "missing_recovery_anchor"
    env.refresh()
    bridge._drive_foreground_cli(started["jobId"], result["cursor"], 0.1)
    assert len(env.calls()) == 1 + bridge.MAX_SESSION_LOOKUP_SCANS
    assert sum('--query' in call['args'] for call in env.calls()) == 1
    assert "stop-chat" not in env.calls()[0]["args"]


@pytest.mark.parametrize("has_anchor", [False, True])
def test_remote_real_sse_tool_failure_uses_foreground_reconnect_only_with_current_anchor(remote_lifecycle, has_anchor):
    env = remote_lifecycle
    started = env.start("sse-lost" if has_anchor else "sse-lost-no-anchor")
    job_id = started["jobId"]
    bridge._drive_foreground_cli(job_id, 0, 5)
    final = bridge._follow_job_local(job_id, 0, 5)
    calls = env.calls()
    assert len(calls) == (2 if has_anchor else 1 + bridge.MAX_SESSION_LOOKUP_SCANS)
    assert all(item["hil"] == "fake-context-one" for item in calls)
    assert all(item["args"][:2] in (["ros", "start-chat"], ["ros", "list-chat-sessions"]) for item in calls)
    if has_anchor:
        assert final["state"] == "turn-completed", final
        assert final["finalText"] == "authoritative final"
        assert ["--query" in item["args"] for item in calls] == [True, False]
        assert _command_body(calls[1]["args"]) == {
            "StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.lifecycle.1",
        }
    else:
        assert final["state"] == "failed"
        assert final["error"]["code"] == "missing_recovery_anchor"
        assert "outcome is unknown" in final["error"]["message"]
        env.refresh()
        bridge._drive_foreground_cli(job_id, final["cursor"], 0.1)
        assert len(env.calls()) == 1 + bridge.MAX_SESSION_LOOKUP_SCANS


def test_remote_abandoned_foreground_before_anchor_closes_cli_without_new_query(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("no-anchor")
    foreground = env.drive(started["jobId"])
    env.wait_for(lambda: len(env.calls()) == 1)
    foreground.terminate()
    foreground.communicate(timeout=5)
    env.refresh()
    bridge._drive_foreground_cli(started["jobId"], 0, 5)
    result = bridge._follow_job_local(started["jobId"], 0, 5)
    assert result["error"]["code"] == "missing_recovery_anchor"
    env.wait_for(lambda: not list(bridge._remote_cli_root(started["jobId"]).glob("remote-cli-*")))
    assert len(env.calls()) == 1 + bridge.MAX_SESSION_LOOKUP_SCANS
    assert all(call['hil'] == 'fake-context-two' for call in env.calls()[1:])
    assert env.calls()[0]["args"][:2] == ["ros", "start-chat"]


def test_remote_manager_stays_busy_without_follow_then_exits_after_completion(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("no-anchor")
    manager = bridge.ensure_manager(1)
    foreground = env.drive(started["jobId"])
    try:
        env.wait_for(lambda: len(env.calls()) == 1)
        time.sleep(1.3)
        assert bridge._pid_alive(manager["pid"])
        assert foreground.poll() is None
        env.context.with_suffix(".finish").touch()
        env.finish_drive(foreground)
        _wait_for_pid_exit(started["workerPid"])
        _wait_for_pid_exit(manager["pid"])
        assert not bridge._pid_alive(started["workerPid"])
        assert not bridge._pid_alive(manager["pid"])
        assert bridge._load_state_json(bridge._job_paths(started["jobId"])[1])["state"] == "turn-completed"
    finally:
        env.context.with_suffix(".finish").touch()
        _wait_for_pid_exit(manager["pid"], timeout=8.0)


def test_remote_parser_waiting_for_follow_does_not_keep_manager_or_old_hil_active(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("no-anchor")
    manager = bridge.ensure_manager(0.2)
    try:
        _wait_for_pid_exit(manager["pid"])
        assert not bridge._pid_alive(manager["pid"])
        assert bridge._pid_alive(started["workerPid"])
        assert env.calls() == []
        # A later command can create a new manager and supply a fresh HIL;
        # the dormant parser has not sent the original Query on its own.
        env.refresh()
        env.context.with_suffix(".finish").touch()
        foreground = env.drive(started["jobId"])
        env.finish_drive(foreground)
        assert len(env.calls()) == 1
        assert env.calls()[0]["hil"] == "fake-context-two"
    finally:
        bridge._cancel_job_local({"jobId": started["jobId"]})


def test_remote_real_80k_array_truncation_preserves_anchor_and_uses_fresh_hil(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("truncated-array")
    job_id = started["jobId"]
    foreground = env.drive(job_id)
    env.wait_for(lambda: bridge._load_state_json(bridge._job_paths(job_id)[1]).get("streamCursor") == "v1.lifecycle.1")
    root, _job_path, spool = bridge._job_paths(job_id)
    foreground.terminate()
    foreground.communicate(timeout=5)
    env.disconnect(job_id)
    first = bridge._follow_job_local(job_id, 0, 0)
    assert first["state"] != "failed"
    assert len(env.calls()) == 1
    env.refresh()
    bridge._drive_foreground_cli(job_id, first["cursor"], 5)
    final = bridge._follow_job_local(job_id, first["cursor"], 5)
    assert final["state"] == "turn-completed", final
    assert final["finalText"] == "authoritative final"
    calls = env.calls()
    assert len(calls) == 2
    assert [item["hil"] for item in calls] == ["fake-context-one", "fake-context-two"]
    assert ["--query" in item["args"] for item in calls] == [True, False]
    assert _command_body(calls[1]["args"]) == {
        "StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.lifecycle.1",
    }
    ids = [item.get("streamEventId") for item in bridge._read_spool(spool)]
    assert ids.count("v1.lifecycle.1") == 1
    assert "v1.lifecycle.99" not in ids


def test_remote_real_disconnects_use_fresh_hil_and_latest_cursor_without_repeating_query(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("multi-window-first")
    job_id = started["jobId"]
    foreground = env.drive(job_id)
    env.wait_for(lambda: bridge._load_state_json(bridge._job_paths(job_id)[1]).get("streamCursor") == "v1.lifecycle.1")
    first = bridge._follow_job_local(job_id, 0, 0)
    foreground.terminate()
    foreground.communicate(timeout=5)
    env.disconnect(job_id)
    env.refresh()
    time.sleep(0.2)
    assert len(env.calls()) == 1  # The background parser cannot dispatch the queued reconnect.
    foreground = env.drive(job_id, first["cursor"])
    env.wait_for(lambda: len(env.calls()) == 2)
    # Event 2 is an ordinary delta and is now deliberately buffered.
    env.wait_for(lambda: any(path.with_suffix(".stdout").stat().st_size > 0
                            for path in bridge._remote_cli_root(job_id).glob("remote-cli-*.request.json")))
    foreground.terminate()
    foreground.communicate(timeout=5)
    env.disconnect(job_id, again=True)
    second = bridge._follow_job_local(job_id, first["cursor"], 0)
    assert second["followTimedOut"] is True
    assert env.calls()[1]["hil"] == "fake-context-two"
    env.refresh("fake-context-three")
    bridge._drive_foreground_cli(job_id, second["cursor"], 5)
    final = bridge._follow_job_local(job_id, second["cursor"], 1)
    assert final["state"] == "turn-completed"
    assert [item["hil"] for item in env.calls()] == ["fake-context-one", "fake-context-two", "fake-context-three"]
    assert ["--query" in item["args"] for item in env.calls()] == [True, False, False]
    assert [_command_body(item["args"])["StreamOptions.Cursor"] for item in env.calls()[1:]] == [
        "v1.lifecycle.1", "v1.lifecycle.2",
    ]


def test_remote_expired_context_is_not_blindly_retried(remote_lifecycle, monkeypatch):
    env = remote_lifecycle
    started = env.start("finished")
    env.refresh()
    monkeypatch.setenv("STAROPS_HIL", "expired-context")
    bridge._drive_foreground_cli(started["jobId"], 0, 3)
    result = bridge._follow_job_local(started["jobId"], 0, 1)
    assert result["error"]["code"] == "aliyun_cli_failed"
    assert "HIL context expired" in result["error"]["message"]
    assert len(env.calls()) == 1
    log = env.workspace / ".ros-agent-debug" / "remote-aliyun-results.jsonl"
    assert "expired-context" not in log.read_text(encoding="utf-8")


def test_remote_cancel_uses_current_context_and_never_dispatches_from_parser(remote_lifecycle):
    env = remote_lifecycle
    started = env.start()
    job_id = started["jobId"]
    foreground = env.drive(job_id)
    env.wait_for(lambda: bridge._load_state_json(bridge._job_paths(job_id)[1]).get("streamCursor") == "v1.lifecycle.1")
    env.refresh()
    root, job_path, _spool = bridge._job_paths(job_id)
    with bridge.StateLock(root / ".job.lock"):
        job = bridge._load_state_json(job_path)
        job["stopRequestedAt"] = int(time.time())
        bridge._atomic_json(job_path, job)
    assert bridge._claim_stop_dispatch(job_id) is None
    canceled = bridge._cancel_job_local({
        "jobId": job_id, "transientEnvironment": {"STAROPS_HIL": "fake-context-two"},
    })
    assert canceled["state"] == "canceled"
    assert env.calls()[-1]["args"][:2] == ["ros", "stop-chat"]
    assert env.calls()[-1]["hil"] == "fake-context-two"
    deadline = time.monotonic() + 3
    while bridge._pid_alive(env.calls()[0]["pid"]):
        assert time.monotonic() < deadline, "The confirmed cancellation did not close the local CLI reader."
        time.sleep(0.02)
    assert len(env.calls()) == 2
    env.finish_drive(foreground)
    bridge._drive_foreground_cli(job_id, 0, 0.1)
    assert len(env.calls()) == 2


def test_remote_cancel_before_first_invocation_does_not_start_a_cloud_session(remote_lifecycle):
    env = remote_lifecycle
    started = env.start()
    result = bridge._cancel_job_local({"jobId": started["jobId"]})
    assert result["state"] == "not-active"
    assert result["stopStatus"] == "NoActiveStream"
    bridge._drive_foreground_cli(started["jobId"], 0, 0.1)
    assert env.calls() == []
    root = bridge._remote_cli_root(started["jobId"])
    deadline = time.monotonic() + 3
    while list(root.glob("remote-cli-*")):
        assert time.monotonic() < deadline, "The canceled parser worker did not release its pending invocation."
        time.sleep(0.02)


@pytest.mark.parametrize("sideband", [False, True])
def test_remote_permission_response_uses_foreground_hil_and_preserves_parent(remote_lifecycle, sideband):
    env = remote_lifecycle
    started = env.start("pipeline-first" if sideband else "finished", "pipeline" if sideband else "normal")
    job_id = started["jobId"]
    foreground = env.drive(job_id)
    env.wait_for(lambda: bridge._load_state_json(bridge._job_paths(job_id)[1]).get("streamCursor") is not None)
    if not sideband:
        env.finish_drive(foreground)
    root, job_path, _spool = bridge._job_paths(job_id)
    permission = {
        "schemaVersion": 1, "kind": "permission", "requestTaskId": "task-1", "contextId": "session-1",
        "inputId": "input-1", "toolUseId": "tool-1", "permissionClass": "sub_pipeline" if sideband else "normal",
    }
    with bridge.StateLock(root / ".job.lock"):
        job = bridge._load_state_json(job_path)
        parent_pid = job.get("workerPid")
        job["state"] = "input-required"
        job["inputRequired"] = permission
        job["pendingPermissions"] = [permission]
        bridge._atomic_json(job_path, job)
    if sideband:
        env.finish_drive(foreground)
    env.refresh()
    responded = bridge._respond_job_local({
        "jobId": job_id, "permissionRef": bridge._permission_ref(permission), "decision": "allow_once",
    })
    if sideband:
        assert bridge._load_state_json(job_path)["workerPid"] == parent_pid
    bridge._drive_foreground_cli(job_id, responded["cursor"], 5)
    result = bridge._follow_job_local(job_id, responded["cursor"], 1)
    assert result["state"] == ("completed" if sideband else "turn-completed")
    assert result["permissionAck"]["accepted"] is True
    response_calls = [item for item in env.calls() if any(
        "IAC_CODE_PERMISSION:" in value for value in item["args"]
    )]
    assert len(response_calls) == 1
    assert response_calls[0]["hil"] == "fake-context-two"
    if sideband:
        assert bridge._load_state_json(job_path)["activeRequestSeq"] == 1
        assert len(env.calls()) >= 2
        assert all("--query" not in call["args"] for call in env.calls()
                   if not any("IAC_CODE_PERMISSION:" in value for value in call["args"]) and call != env.calls()[0])
    before_duplicate = len(env.calls())
    duplicate = bridge._respond_job_local({"jobId": job_id, "decision": "allow_once"})
    assert duplicate["duplicate"] is True
    assert len(env.calls()) == before_duplicate


@pytest.mark.parametrize("kind", ["permission", "ask_user_question", "candidate_selection"])
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("wrapped_worker", [False, True])
def test_remote_serial_input_releases_reader_before_reply(
    remote_lifecycle, monkeypatch, kind, truncated, wrapped_worker,
):
    env = remote_lifecycle
    original_popen = subprocess.Popen

    def launch(command, *args, **kwargs):
        if wrapped_worker and len(command) > 2 and command[2] == "_worker":
            # Model a Windows venv redirector: Popen.pid belongs to a launcher,
            # while the worker runs under a distinct child PID.
            command = [sys.executable, "-c", "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:]))"] + command
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    started = env.start("input-" + kind + ("-truncated" if truncated else ""))
    job_id = started["jobId"]
    foreground = env.drive(job_id)
    env.finish_drive(foreground)
    result = bridge._follow_job_local(job_id, 0, 0)
    job_path = bridge._job_paths(job_id)[1]
    job = bridge._load_state_json(job_path)
    assert result["state"] == "input-required"
    assert result["inputRequired"]["kind"] == kind
    assert job.get("workerPid") is None
    assert job["streamCursor"] == "v1.input.1"
    assert not list(bridge._remote_cli_root(job_id).glob("remote-cli-*"))
    time.sleep(0.1)  # Later approval is a new foreground/HIL, not the old parent.
    env.refresh()
    if kind == "permission":
        reply = bridge._respond_job_local({"jobId": job_id, "decision": "allow_once"})
    else:
        reply = bridge._continue_job_local({"jobId": job_id, "prompt": "A"})
    bridge._drive_foreground_cli(job_id, reply["cursor"], 5)
    final = bridge._follow_job_local(job_id, reply["cursor"], 1)
    assert final["state"] == "turn-completed", final
    assert [call["hil"] for call in env.calls()] == ["fake-context-one", "fake-context-two"]
    assert len(env.calls()) == 2
    assert all("--query" in call["args"] for call in env.calls())
    assert not any("stop-chat" in call["args"] for call in env.calls())


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
@pytest.mark.parametrize("kind", ["ask_user_question", "candidate_selection"])
@pytest.mark.parametrize("has_anchor", [False, True])
def test_remote_answer_sse_loss_uses_only_current_request_anchor(remote_lifecycle, mode, kind, has_anchor):
    env = remote_lifecycle
    started = env.start("input-" + kind, mode)
    job_id = started["jobId"]
    env.finish_drive(env.drive(job_id))
    first = bridge._follow_job_local(job_id, 0, 0)
    job_path = bridge._job_paths(job_id)[1]
    assert first["inputRequired"]["kind"] == kind
    assert first["sessionId"] == "session-1"
    assert bridge._load_state_json(job_path)["streamCursor"] == "v1.input.1"

    env.refresh()
    answer = "sse-lost" if has_anchor else "sse-lost-no-anchor"
    reply = bridge._continue_job_local({"jobId": job_id, "prompt": answer})
    cursor = reply["cursor"]
    for _ in range(3):
        # Missing-anchor recovery adds up to three real CLI list invocations.
        # Leave the idle window unchanged; only scale this test's wall watchdog.
        env.finish_drive(env.drive(job_id, cursor, seconds=1), timeout=8 if has_anchor else 20)
        final = bridge._follow_job_local(job_id, cursor, 0)
        if final["state"] in {"failed", "completed", "turn-completed"}:
            break
        cursor = final["cursor"]
    assert final["jobId"] == job_id
    assert final["sessionId"] == "session-1"
    calls = env.calls()
    assert len(calls) == (3 if has_anchor else 2 + bridge.MAX_SESSION_LOOKUP_SCANS)
    assert all(call["args"][:2] in (["ros", "start-chat"], ["ros", "list-chat-sessions"]) for call in calls)
    assert ["--query" in call["args"] for call in calls] == (
        [True, True, False] if has_anchor else [True, True] + [False] * bridge.MAX_SESSION_LOOKUP_SCANS
    )
    assert calls[1]["args"][calls[1]["args"].index("--query") + 1] == answer
    assert calls[1]["args"][calls[1]["args"].index("--session-id") + 1] == "session-1"
    assert all(call["hil"] == "fake-context-two" for call in calls[1:])
    if has_anchor:
        assert final["state"] == ("completed" if mode == "pipeline" else "turn-completed"), final
        assert _command_body(calls[2]["args"]) == {
            "StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.lifecycle.1",
        }
    else:
        assert final["state"] == "failed", final
        assert final["error"]["code"] == "missing_recovery_anchor"
        assert final["error"]["retryable"] is False
        assert "outcome is unknown" in final["error"]["message"]
    env.refresh("fake-context-three")
    bridge._drive_foreground_cli(job_id, final["cursor"], 0.1)
    assert len(env.calls()) == len(calls)


@pytest.mark.parametrize("kind", ["initial", "ask_user_question", "candidate_selection", "allow_once", "deny"])
def test_remote_first_cursor_recovers_initial_question_and_permission_loss(remote_lifecycle, kind):
    env = remote_lifecycle
    permission = kind in {"allow_once", "deny"}
    started = env.start("sse-lost-no-anchor" if kind == "initial" else (
        "input-permission" if permission else "input-" + kind
    ))
    job_id = started["jobId"]
    job_path = bridge._job_paths(job_id)[1]
    job = bridge._load_state_json(job_path)
    assert job["clientReferenceId"] == job_id
    env.context.with_suffix('.session-list.json').write_text(json.dumps({"Sessions": [
        {"SessionId": "unrelated", "ClientReferenceId": "other", "Description": "lookup-private-other-session"},
        {"SessionId": "session-1", "ClientReferenceId": job_id,
         "FirstCursor": "v1.permission.0" if permission else "v1.lifecycle.0"},
    ]}), encoding="utf-8")
    if kind != "initial":
        env.finish_drive(env.drive(job_id))
        env.refresh()
        if permission:
            env.context.with_suffix('.permission-loss').touch()
            response = bridge._respond_job_local({"jobId": job_id, "decision": kind})
        else:
            response = bridge._continue_job_local({"jobId": job_id, "prompt": "sse-lost-no-anchor"})
        cursor = response["cursor"]
    else:
        cursor = 0
    env.finish_drive(env.drive(job_id, cursor, seconds=5), timeout=20)
    final = bridge._follow_job_local(job_id, cursor, 1)
    assert final["state"] == "turn-completed", final
    assert final["sessionId"] == "session-1"
    assert final["finalText"] == "authoritative final"
    if permission:
        assert final["permissionAck"]["accepted"] is True
        assert final["permissionAck"]["decision"] == kind
    calls = env.calls()
    assert [call["args"][1] for call in calls[-2:]] == ["list-chat-sessions", "start-chat"]
    assert sum("--query" in call["args"] for call in calls) == (1 if kind == "initial" else 2)
    assert _command_body(calls[-1]["args"])["StreamOptions.Cursor"] == (
        "v1.permission.0" if permission else "v1.lifecycle.0"
    )
    assert all(call["hil"] == ("fake-context-one" if kind == "initial" else "fake-context-two")
               for call in calls[-3:])
    assert all("stop-chat" not in call["args"] for call in calls)
    assert bridge._load_state_json(job_path)["streamCursor"] == (
        "v1.permission.2" if permission else "v1.lifecycle.2"
    )
    log_path = env.workspace / ".ros-agent-debug/remote-aliyun-results.jsonl"
    assert "lookup-private-other-session" not in log_path.read_text(encoding="utf-8")


def test_agenthub_packaged_first_cursor_recovery_across_independent_foregrounds(remote_lifecycle, monkeypatch):
    env = remote_lifecycle
    installed = env.workspace.parent / "agenthub-skill"
    subprocess.run([
        sys.executable, str(ROOT / "skill-runtime/skill_profiles.py"),
        "--profile", "alibabacloud-ros-agent", "--output", str(installed),
    ], check=True, capture_output=True)
    script = installed / "scripts/ros_agent.py"
    monkeypatch.setattr(sys.modules[__name__], "BRIDGE_PATH", script)
    monkeypatch.setattr(bridge, "__file__", str(script))
    # Each independent host command may have a fresh telemetry session ID.
    # Inherited host/user configuration must not hide that process boundary.
    monkeypatch.delenv("SKILL_SESSION_ID", raising=False)
    started = env.start("sse-lost-no-anchor")
    job_id = started["jobId"]
    env.context.with_suffix(".session-list.json").write_text(json.dumps({"Sessions": [
        {"SessionId": "session-1", "ClientReferenceId": job_id, "FirstCursor": "v1.lifecycle.0"},
    ]}), encoding="utf-8")
    env.finish_drive(env.drive(job_id, seconds=5), timeout=20)
    first = bridge._follow_job_local(job_id, 0, 1)
    assert first["state"] == "turn-completed", first
    env.refresh()
    reply = bridge._continue_job_local({"jobId": job_id, "prompt": "sse-lost-no-anchor"})
    env.finish_drive(env.drive(job_id, reply["cursor"], seconds=5), timeout=20)
    final = bridge._follow_job_local(job_id, reply["cursor"], 1)
    assert final["state"] == "turn-completed", final
    assert final["sessionId"] == first["sessionId"] == "session-1"
    calls = env.calls()
    assert [call["args"][1] for call in calls] == ["start-chat", "list-chat-sessions", "start-chat"] * 2
    assert sum("--query" in call["args"] for call in calls) == 2
    headers = [call["args"][call["args"].index("--user-agent") + 1] for call in calls]
    assert all(header.startswith("AlibabaCloud-Agent-Skills/alibabacloud-ros-agent/") for header in headers)
    assert all(header.endswith(" skill-version/0.5.0") for header in headers)
    assert headers[0] != headers[3]
    assert all(call["hil"] == "fake-context-two" for call in calls[3:])


def test_remote_answer_receipt_does_not_retire_reader_before_second_question(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("input-ask_user_question", "pipeline")
    job_id = started["jobId"]
    env.finish_drive(env.drive(job_id))
    first = bridge._follow_job_local(job_id, 0, 0)
    assert first["inputRequired"]["inputId"] == "input-1"

    env.refresh()
    reply = bridge._continue_job_local({"jobId": job_id, "prompt": "receipt-then-input"})
    env.finish_drive(env.drive(job_id, reply["cursor"]))
    second = bridge._follow_job_local(job_id, reply["cursor"], 0)
    assert second["state"] == "input-required"
    assert second["inputRequired"]["inputId"] == "input-2"
    assert second["inputRequired"]["prompt"] == "Development or production?"
    assert second["sessionId"] == first["sessionId"] == "session-1"

    env.refresh("fake-context-three")
    reply = bridge._continue_job_local({"jobId": job_id, "prompt": "Development"})
    env.finish_drive(env.drive(job_id, reply["cursor"]))
    final = bridge._follow_job_local(job_id, reply["cursor"], 0)
    assert final["state"] == "completed"
    assert final["jobId"] == job_id
    assert "inputRequired" not in final
    calls = env.calls()
    assert len(calls) == 3
    assert [item["hil"] for item in calls] == ["fake-context-one", "fake-context-two", "fake-context-three"]
    assert not any(item["args"][:2] == ["ros", "stop-chat"] for item in calls)


@pytest.mark.parametrize("mailbox_removed", [False, True])
def test_remote_input_handoff_timeout_starts_at_boundary_not_cli_launch(monkeypatch, tmp_path, mailbox_removed):
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    job_id = "a" * 32
    root, job_path, _spool = bridge._job_paths(job_id)
    bridge._secure_directory(root)
    job = {"aliyunPath": "/fake/aliyun", "state": "working", "workerPid": 42}
    bridge._atomic_json(job_path, job)
    request_path = bridge._remote_cli_root(job_id) / "remote-cli-test.request.json"
    bridge._atomic_json(request_path, {
        "command": ["/fake/aliyun", "ros", "start-chat"], "parserPid": 43,
    })
    clock = [0.0]
    observations = []

    def observe(_job_id, _cursor):
        observations.append(clock[0])
        if len(observations) == 1:
            return None, dict(job)
        boundary = dict(job, state="input-required", inputRequired={"kind": "permission"})
        bridge._atomic_json(request_path.with_suffix(".result"), {"returnCode": 0})
        if mailbox_removed:
            request_path.unlink(missing_ok=True)
        if len(observations) >= 3:
            boundary.pop("workerPid")
        return {"state": "input-required"}, boundary

    def launch(*_args, **_kwargs):
        request = bridge._load_state_json(request_path)
        request["cliPid"] = 44
        bridge._atomic_json(request_path, request)
        return SimpleNamespace(poll=lambda: None)

    def advance(_seconds):
        # The cloud input arrives after the 10-second process-launch deadline.
        # Reader cleanup then takes another observation before releasing ownership.
        clock[0] += 20.0 if clock[0] == 0.0 else 1.0

    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge.time, "sleep", advance)
    monkeypatch.setattr(bridge, "_pid_alive", lambda pid: pid in {42, 43, 44})
    monkeypatch.setattr(bridge, "_follow_ready_result", observe)
    monkeypatch.setattr(bridge, "_remote_cli_subprocess_environment", lambda *_args: {})
    monkeypatch.setattr(bridge, "_capture_remote_cli_environment", lambda *_args: {})
    monkeypatch.setattr(bridge.subprocess, "Popen", launch)
    bridge._drive_foreground_cli(job_id, 0, 5)
    assert observations == [0.0, 20.0, 21.0]


@pytest.mark.parametrize("kind", ["permission", "ask_user_question", "candidate_selection"])
@pytest.mark.parametrize("cursor", [0, 1000])
def test_remote_orphan_serial_mailbox_is_retired_without_launching_old_query(remote_lifecycle, kind, cursor):
    env = remote_lifecycle
    started = env.start("no-anchor")
    job_id = started["jobId"]
    root, job_path, _spool = bridge._job_paths(job_id)
    pending = {
        "schemaVersion": 1, "kind": kind, "requestTaskId": "task-1", "contextId": "session-1",
        "inputId": "input-1", "prompt": "Choose A",
    }
    if kind == "permission":
        pending.update(toolUseId="tool-1", permissionClass="normal")
    # Reconstruct the observed live state: a committed input and an unclaimed
    # mailbox. The original cloud sequence is intentionally not assumed here.
    with bridge.StateLock(root / ".job.lock"):
        job = bridge._load_state_json(job_path)
        job.update(state="input-required", sessionId="session-1", inputRequired=pending)
        bridge._atomic_json(job_path, job)
    env.finish_drive(env.drive(job_id, cursor))
    assert bridge._load_state_json(job_path).get("workerPid") is None
    assert env.calls() == []
    assert not list(bridge._remote_cli_root(job_id).glob("remote-cli-*"))
    env.refresh()
    if kind == "permission":
        reply = bridge._respond_job_local({"jobId": job_id, "decision": "deny"})
    else:
        reply = bridge._continue_job_local({"jobId": job_id, "prompt": "A"})
    bridge._drive_foreground_cli(job_id, reply["cursor"], 5)
    final = bridge._follow_job_local(job_id, reply["cursor"], 1)
    assert final["state"] == "turn-completed", final
    assert len(env.calls()) == 1
    assert env.calls()[0]["hil"] == "fake-context-two"
    assert "no-anchor" not in env.calls()[0]["args"]


def test_normal_pending_permission_snapshot_is_serial(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/fake/aliyun")
    envelope = {
        "schemaVersion": 1, "kind": "permission", "requestTaskId": "task-1", "contextId": "session-1",
        "inputId": "input-1", "toolUseId": "tool-1",
    }
    payload = _status_event(metadata={"pendingPermissions": [envelope]})
    calls = []
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda cmd, **_kw: (
        calls.append(cmd) or _FakeCLIProcess(json.dumps({"id": "v1.pending.1", "data": payload}))
    ))
    result = bridge._consume_start_chat(_chat_args(transport="aliyun_cli"), tmp_path, "test", None, [])
    assert result["inputRequired"]["permissionClass"] == "normal"
    assert result["state"] == "input-required"
    assert len(calls) == 1


def test_remote_repeated_mixed_inputs_keep_one_job_and_session(remote_lifecycle):
    env = remote_lifecycle
    started = env.start("input-ask_user_question")
    job_id = started["jobId"]
    env.finish_drive(env.drive(job_id))
    first = bridge._follow_job_local(job_id, 0, 0)
    assert first["inputRequired"]["kind"] == "ask_user_question"
    for number, kind in enumerate(("permission", "candidate_selection", "ask_user_question"), 2):
        env.refresh("fake-context-" + str(number))
        reply = bridge._continue_job_local({"jobId": job_id, "prompt": "input-" + kind})
        env.finish_drive(env.drive(job_id, reply["cursor"]))
        result = bridge._follow_job_local(job_id, reply["cursor"], 0)
        assert result["jobId"] == job_id
        assert result["sessionId"] == "session-1"
        assert result["inputRequired"]["kind"] == kind
        assert bridge._load_state_json(bridge._job_paths(job_id)[1]).get("workerPid") is None
        if kind == "permission":
            env.refresh("fake-context-permission")
            response = bridge._respond_job_local({"jobId": job_id, "decision": "deny"})
            env.finish_drive(env.drive(job_id, response["cursor"]))
            assert bridge._follow_job_local(job_id, response["cursor"], 0)["state"] == "turn-completed"
    env.refresh("fake-context-final")
    reply = bridge._continue_job_local({"jobId": job_id, "prompt": "A"})
    env.finish_drive(env.drive(job_id, reply["cursor"]))
    assert bridge._follow_job_local(job_id, reply["cursor"], 0)["state"] == "turn-completed"
    assert len(env.calls()) == 6
    assert all("--query" in call["args"] for call in env.calls())


@pytest.mark.parametrize("terminal", [False, True])
def test_unknown_input_fails_explicitly_but_cannot_reopen_terminal_state(terminal):
    summary = bridge.StreamSummary("session-1", mode="normal")
    if terminal:
        summary.apply(_status_event(state="TASK_STATE_COMPLETED"))
    payload = _status_event(state="TASK_STATE_INPUT_REQUIRED", metadata={
        "input": {"kind": "future-input", "inputId": "input-unknown", "prompt": "Do not assume completion"},
    })
    if terminal:
        summary.apply(payload)
        assert summary.to_result(0, "")["state"] == "turn-completed"
        assert summary.input_required is None
    else:
        with pytest.raises(bridge.BridgeError) as error:
            summary.apply(payload)
        assert error.value.code == "unsupported_input"
