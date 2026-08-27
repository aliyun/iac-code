from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = ROOT / "skills/alicloud-ros-agent/scripts/ros_agent.py"


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


def _clear_code_credential_env(monkeypatch) -> None:
    for name in bridge.ACCESS_KEY_ID_ENV_NAMES + bridge.ACCESS_KEY_SECRET_ENV_NAMES + bridge.SECURITY_TOKEN_ENV_NAMES:
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
    source = BRIDGE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    imported_modules = {
        alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
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
        "time",
        "typing",
        "urllib",
        "uuid",
    }
    assert "access-key-id" not in source.lower()
    assert "access-key-secret" not in source.lower()


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


def test_managed_start_persists_effective_environment_identity_region_and_thinking(monkeypatch, tmp_path: Path) -> None:
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
    assert captured["payload"]["credentialSource"] == "environment"
    assert captured["payload"]["regionId"] == "cn-shenzhen"
    assert captured["payload"]["noThinking"] is True


def test_check_returns_safe_current_profile_and_effective_skill_policy(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda profile: {
            "name": profile or "test-profile",
            "mode": "OAuth",
            "language": "zh",
            "regionId": "cn-hangzhou",
        },
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
        "aliyunCLIProfile": "",
        "currentProfile": {
            "configured": True,
            "name": "test-profile",
            "mode": "OAuth",
            "language": "zh",
            "regionId": "cn-hangzhou",
        },
    }
    assert captured == {
        "sdk": {"sdk": True},
        "aliyunPath": "aliyun",
        "profile": "test-profile",
        "regionId": "cn-hangzhou",
        "credentialSource": "profile",
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
    bridge.apply_skill_config(args, {})

    with pytest.raises(bridge.BridgeError) as error:
        bridge.run_check(args)
    assert error.value.code == "credential_failed"


def test_code_check_prefers_cli_compatible_environment_credentials(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALIBABA_CLOUD_SECURITY_TOKEN", "fake-env-token")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-shanghai")
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: pytest.fail("environment mode must not require CLI"))
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: {})
    args = argparse.Namespace(command="check", aliyun_path="aliyun")
    bridge.apply_skill_config(args, {})

    result = bridge.run_check(args)

    assert result["currentProfile"] == {
        "configured": True,
        "mode": "Environment",
        "regionId": "cn-shanghai",
    }
    assert result["cli"] is None
    assert result["version"] is None
    assert "fake-env" not in json.dumps(result)


def test_environment_credential_alias_order_matches_aliyun_cli_and_partial_values_fail(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "first-ak")
    monkeypatch.setenv("ACCESS_KEY_ID", "last-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "first-secret")
    monkeypatch.setenv("ACCESS_KEY_SECRET", "last-secret")
    monkeypatch.setenv("ALICLOUD_SECURITY_TOKEN", "token")

    assert bridge._environment_credentials() == ("first-ak", "first-secret", "token")

    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_ID", "orphan-ak")
    with pytest.raises(bridge.BridgeError) as error:
        bridge._environment_credentials()
    assert error.value.code == "credential_failed"


def test_code_start_identity_uses_environment_region_without_requiring_cli(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    _clear_region_and_profile_env(monkeypatch)
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALIBABACLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALIBABACLOUD_REGION_ID", "cn-shanghai")
    monkeypatch.setattr(
        bridge,
        "_selected_cli_profile_record",
        lambda _profile: pytest.fail("environment credentials must not require a CLI Profile"),
    )
    args = SimpleNamespace(
        transport="code",
        profile=None,
        profile_pinned=False,
        region_id=None,
    )

    bridge._resolve_start_identity(args)

    assert args.profile is None
    assert args.credential_source == "environment"
    assert args.region_id == "cn-shanghai"


def test_code_start_identity_defaults_environment_region_to_hangzhou(monkeypatch) -> None:
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

    assert args.credential_source == "environment"
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
        lambda: {"installed": True, "ready": True, "version": "0.7.2"},
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
    assert result["rosPluginVersion"] == "0.7.2"


@pytest.mark.parametrize(
    ("plugin_status", "auto_install", "install_required"),
    [
        ({"installed": False, "ready": False}, False, True),
        ({"installed": False, "ready": False}, True, False),
        ({"installed": True, "ready": False, "version": "0.7.1"}, True, True),
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

    assert result["rosPluginReady"] is False
    assert result["pluginAutoInstallEnabled"] is auto_install
    assert result["pluginInstallRequired"] is install_required


def test_remote_aliyun_cli_check_does_not_run_cli_or_read_local_configuration(monkeypatch) -> None:
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
            "endpoint": "ros-pre.aliyuncs.com",
        },
    )

    result = bridge.run_check(args)

    assert result["ok"] is True
    assert result["cli"] == "aliyun"
    assert result["version"] is None
    assert result["aliyunCLIExecutionMode"] == "remote"
    assert result["currentProfile"] == {"configured": True, "mode": "RemoteSandbox"}
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

    assert bridge._local_ros_plugin_status() == {"installed": True, "ready": True, "version": "0.7.2"}

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
    assert bridge._local_ros_plugin_status() == {"installed": True, "ready": False, "version": "0.7.1"}


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


def test_sse_parser_handles_heartbeats_multiline_data_and_raw_json() -> None:
    lines = [
        ": comment\n",
        'data: {"object":"heartbeat"}\n',
        "\n",
        'data: {"value":\n',
        "data: 1}\n",
        "\n",
        '{"result":{"ok":true}}\n',
    ]
    events = list(bridge.iter_sse_payloads(lines))
    assert [event[0] for event in events] == [
        {"object": "heartbeat"},
        {"value": 1},
        {"result": {"ok": True}},
    ]


def test_cli_plugin_parser_streams_and_unwraps_each_json_line() -> None:
    first = {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}}
    second = {"result": {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}}

    events = list(
        bridge.iter_cli_plugin_payloads(
            [
                json.dumps({"data": first}) + "\n",
                json.dumps({"data": second}) + "\n",
            ]
        )
    )

    assert events == [(first, json.dumps({"data": first})), (second, json.dumps({"data": second}))]


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


def test_run_chat_consumes_fake_cli_stream_without_network(monkeypatch, tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello", encoding="utf-8")
    output = json.dumps(
        {
            "data": _status_event(
                state="TASK_STATE_INPUT_REQUIRED",
                text="done",
                metadata={"assistantFinal": {"complete": True}},
            )
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
            self.lines = iter([("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()])

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

    class FakeCredentials:
        def get_access_key_id(self):
            return "fake-ak"

        def get_access_key_secret(self):
            return "fake-secret"

        def get_security_token(self):
            return "fake-token"

    class FakeProvider:
        def __init__(self, profile_name=None):
            captured["profile"] = profile_name

        def get_credentials(self):
            return FakeCredentials()

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
    sdk["CLIProfileCredentialsProvider"] = FakeProvider
    sdk["requests"] = SimpleNamespace(Session=FakeSession)
    monkeypatch.setattr(sdk["OpenApiUtils"], "get_timestamp", staticmethod(lambda: "2026-08-26T03:00:00Z"))
    monkeypatch.setattr(sdk["OpenApiUtils"], "get_nonce", staticmethod(lambda: "fixed-nonce"))
    monkeypatch.setattr(bridge, "_load_code_sdk", lambda: sdk)
    monkeypatch.setattr(bridge, "_selected_cli_profile", lambda profile: (profile, "AK"))

    response = bridge._open_code_request(
        "StartChat",
        {"AgentVersion": "V2", "Query": "hello"},
        "127.0.0.1:56124",
        "skill-profile",
        "cn-hangzhou",
        "aliyun",
        10,
        600,
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


def test_code_credentials_use_environment_before_cli_profile(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    monkeypatch.setenv("ALICLOUD_SECURITY_TOKEN", "fake-env-token")
    sdk = {
        "CLIProfileCredentialsProvider": lambda **_kwargs: pytest.fail("environment credentials must win"),
    }
    monkeypatch.setattr(bridge, "_selected_cli_profile", lambda *_args: pytest.fail("must not inspect Profile"))

    credentials = bridge._code_credentials(sdk, "aliyun", "ignored-profile", "cn-hangzhou")

    assert credentials == ("fake-env-ak", "fake-env-secret", "fake-env-token")


def test_code_credentials_with_profile_source_do_not_fall_back_to_environment(monkeypatch) -> None:
    _clear_code_credential_env(monkeypatch)
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_ID", "fake-env-ak")
    monkeypatch.setenv("ALICLOUD_ACCESS_KEY_SECRET", "fake-env-secret")
    captured = {}

    class FakeCredentials:
        def get_access_key_id(self):
            return "fake-profile-ak"

        def get_access_key_secret(self):
            return "fake-profile-secret"

        def get_security_token(self):
            return None

    class FakeProvider:
        def __init__(self, profile_name=None):
            captured["profile"] = profile_name

        def get_credentials(self):
            return FakeCredentials()

    sdk = {
        "CLIProfileCredentialsProvider": FakeProvider,
    }
    monkeypatch.setattr(bridge, "_selected_cli_profile", lambda profile: (profile, "AK"))

    credentials = bridge._code_credentials(
        sdk,
        "aliyun",
        "fixed-profile",
        "cn-hangzhou",
        "profile",
    )

    assert captured["profile"] == "fixed-profile"
    assert credentials == ("fake-profile-ak", "fake-profile-secret", None)


def test_code_credentials_delegate_oauth_refresh_to_native_cli(monkeypatch, tmp_path: Path) -> None:
    _clear_code_credential_env(monkeypatch)
    config_path = tmp_path / "config.json"
    commands = []
    config_path.write_text(
        json.dumps(
            {
                "current": "oauth-profile",
                "profiles": [
                    {
                        "name": "oauth-profile",
                        "mode": "OAuth",
                        "access_key_id": "fake-expired-ak",
                        "access_key_secret": "fake-expired-secret",
                        "sts_token": "fake-expired-token",
                        "sts_expiration": int(time.time()) - 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        assert command[1:3] == ["ros", "DescribeRegions"]
        config_path.write_text(
            json.dumps(
                {
                    "current": "oauth-profile",
                    "profiles": [
                        {
                            "name": "oauth-profile",
                            "mode": "OAuth",
                            "access_key_id": "fake-refreshed-ak",
                            "access_key_secret": "fake-refreshed-secret",
                            "sts_token": "fake-refreshed-token",
                            "sts_expiration": int(time.time()) + 3600,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    sdk = {
        "CLIProfileCredentialsProvider": lambda **_kwargs: pytest.fail("OAuth must be refreshed by native CLI"),
    }
    monkeypatch.setattr(bridge, "resolve_aliyun", lambda _path: "/usr/local/bin/aliyun")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge, "_cli_config_path", lambda: config_path)

    credentials = bridge._code_credentials(sdk, "aliyun", "oauth-profile", "cn-hangzhou")

    assert credentials == ("fake-refreshed-ak", "fake-refreshed-secret", "fake-refreshed-token")
    assert len(commands) == 1
    refresh_command, refresh_options = commands[0]
    assert "--dryrun" in refresh_command
    assert refresh_command[refresh_command.index("--profile") + 1] == "oauth-profile"
    assert refresh_command[refresh_command.index("--region") + 1] == "cn-hangzhou"
    assert refresh_command[refresh_command.index("--user-agent") + 1] == bridge.USER_AGENT
    assert refresh_options["stdout"] == bridge.subprocess.DEVNULL
    assert refresh_options["stderr"] == bridge.subprocess.DEVNULL


def test_code_credentials_reuse_unexpired_oauth_sts_without_starting_cli(monkeypatch, tmp_path: Path) -> None:
    _clear_code_credential_env(monkeypatch)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "current": "oauth-profile",
                "profiles": [
                    {
                        "name": "oauth-profile",
                        "mode": "OAuth",
                        "access_key_id": "fake-cached-ak",
                        "access_key_secret": "fake-cached-secret",
                        "sts_token": "fake-cached-token",
                        "sts_expiration": int(time.time()) + 3600,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sdk = {
        "CLIProfileCredentialsProvider": lambda **_kwargs: pytest.fail("OAuth must not use SDK refresh"),
    }
    monkeypatch.setattr(bridge, "_cli_config_path", lambda: config_path)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("CLI must not start"))

    credentials = bridge._code_credentials(sdk, "aliyun", None, "cn-hangzhou")

    assert credentials == ("fake-cached-ak", "fake-cached-secret", "fake-cached-token")


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
    output = json.dumps(
        {
            "data": {
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
                },
            }
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

    assert result["state"] == "permission-responded"
    assert result["permissionResponse"]["decision"] == "deny"
    assert query["decision"] == "deny"
    assert command[command.index("--enable-thinking") + 1] == "false"
    assert "--client-context" not in command
    assert "--attachments" not in command


def _wait_for_pid_exit(pid: int, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and bridge._pid_alive(pid):
        time.sleep(0.05)


def test_manager_is_loopback_authenticated_reused_and_recovers_after_idle_shutdown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(bridge, "MANAGER_IDLE_SECONDS", 0.3)

    first = bridge.ensure_manager()
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


def test_manager_idle_countdown_starts_after_sse_worker_exits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bridge.STATE_DIR_ENV, str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Invoke the current interpreter as the fake CLI and let it execute the
    # positional ``ros`` script from the worker cwd. This avoids depending on
    # Windows batch-file launch behavior in a manager lifecycle test.
    fake_cli = Path(sys.executable)
    (workspace / "ros").write_text(
        "import json, time\n"
        + "time.sleep(0.6)\n"
        + "event = {'result': {'statusUpdate': {'taskId': 'task-1', 'contextId': 'session-1', "
        + "'status': {'state': 'TASK_STATE_INPUT_REQUIRED', 'message': {'role': 'ROLE_AGENT', "
        + "'parts': [{'text': 'done'}]}}, 'metadata': {'iac_code': {'assistantFinal': "
        + "{'complete': True}}, 'iacCodeSessionId': 'iac-1'}}}}\n"
        + "print(json.dumps({'data': event}), flush=True)\n",
        encoding="utf-8",
    )

    # Leave enough scheduling headroom for a loaded Windows xdist runner; this
    # test is about when the idle countdown starts, not sub-second timing.
    manager = bridge.ensure_manager(5.0)
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
    time.sleep(0.35)
    assert bridge._pid_alive(manager["pid"])

    _root, job_path, _spool = bridge._job_paths(started["jobId"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not isinstance(bridge._load_state_json(job_path).get("workerPid"), int):
            break
        time.sleep(0.02)
    assert bridge._load_state_json(job_path)["state"] == "turn-completed"
    assert bridge._pid_alive(manager["pid"])
    time.sleep(0.08)
    assert bridge._pid_alive(manager["pid"])

    _wait_for_pid_exit(manager["pid"], timeout=8.0)
    assert not bridge._pid_alive(manager["pid"])


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
        + "def emit(value):\n"
        + "    print(json.dumps({'data': value}), flush=True)\n"
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
        ("Stopping", "canceling", "working", True),
        ("NoActiveStream", "not-active", "working", True),
        ("Failed", "cancel-failed", "working", False),
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
        assert job["inputRequired"]["kind"] == "permission"


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
            "endpoint": "ros-pre.aliyuncs.com",
            "regionId": None,
            "profile": None,
            "credentialSource": "remote",
            "aliyunPath": "aliyun",
        }
    )

    _root, job_path, _spool = bridge._job_paths(started["jobId"])
    job = bridge._load_state_json(job_path)
    assert job["aliyunCLIExecutionMode"] == "remote"
    assert job["profile"] is None
    assert job["regionId"] is None
    assert captured["request"]["aliyunCLIExecutionMode"] == "remote"
    assert captured["request"]["credentialSource"] == "remote"


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


def test_managed_respond_waits_for_top_pipeline_parent_worker_to_reach_eof(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(
        bridge,
        "_spawn_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("top permission must wait for parent EOF")),
    )

    with pytest.raises(bridge.BridgeError) as error:
        bridge._respond_job_local({"jobId": job_id, "inputFile": str(permission_path), "decision": "allow_once"})
    job = bridge._load_state_json(job_path)

    assert error.value.code == "job_busy"
    assert error.value.retryable is True
    assert job["workerPid"] == 4321
    assert job["activeRequestSeq"] == 1
    assert job["state"] == "input-required"
    assert job["inputRequired"] == pending
    assert "lastPermissionResponse" not in job


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
