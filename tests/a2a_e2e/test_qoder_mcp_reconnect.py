from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "a2a" / "e2e" / "reconnect" / "run_qoder_mcp_reconnect.py"
FAKE_CLI_PATH = REPO_ROOT / "scripts" / "a2a" / "e2e" / "reconnect" / "fake_aliyun_cli.py"
MCP_SERVER_PATH = REPO_ROOT / "scripts" / "a2a" / "e2e" / "reconnect" / "aliyun_cli_mcp_server.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runner():
    return _load("qoder_mcp_reconnect_runner", RUNNER_PATH)


def _mcp_server():
    return _load("qoder_mcp_reconnect_server", MCP_SERVER_PATH)


def _fake_cli():
    return _load("qoder_mcp_reconnect_fake_cli", FAKE_CLI_PATH)


def test_runner_requires_explicit_real_cloud_opt_in(tmp_path) -> None:
    runner = _runner()

    with pytest.raises(SystemExit, match="--allow-real-cloud"):
        runner._preflight(SimpleNamespace(allow_real_cloud=False), tmp_path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.9.0", (0, 9, 0)), ("0.9.1", (0, 9, 1)), ("0.10.0", (0, 10, 0)), ("latest", None)],
)
def test_runner_parses_stable_ros_plugin_versions(value, expected) -> None:
    assert _runner()._plugin_version(value) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python /tmp/alicloud-ros-agent/scripts/ros_agent.py check", ["check"]),
        ("ALICLOUD_ROS_AGENT_STATE_DIR=/tmp/state python '/tmp/ros_agent.py' start --mode normal", ["start"]),
        ("python /tmp/ros_agent.py follow --job-id fixture", ["follow"]),
        (
            "python /tmp/ros_agent.py start --mode normal; python /tmp/ros_agent.py follow --job-id fixture",
            ["start", "follow"],
        ),
        ("rg -n ros_agent.py /tmp/alicloud-ros-agent/SKILL.md", []),
        ("sed -n '1,80p' /tmp/alicloud-ros-agent/scripts/ros_agent.py", []),
    ],
)
def test_runner_counts_only_actual_bridge_invocations(command, expected) -> None:
    _command, subcommands = _runner()._bridge_subcommands(
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    )

    assert subcommands == expected


def test_installed_skill_is_temporarily_patched_to_remote_fake_cli(tmp_path) -> None:
    runner = _runner()
    skill_root = tmp_path / "qoder-skills"
    destination = skill_root / "alicloud-ros-agent"
    destination.mkdir(parents=True)
    (destination / "old.txt").write_text("old", encoding="utf-8")
    fake_cli = tmp_path / "fake-aliyun"
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")

    backups = runner._install_patched_skill(REPO_ROOT, [skill_root], fake_cli)

    config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
    runtime = (destination / "scripts" / "_ros_agent_runtime.py").read_text(encoding="utf-8")
    assert config["transport"] == "aliyun_cli"
    assert config["aliyunCLIExecutionMode"] == "remote"
    assert config["endpoint"] == "ros-pre.aliyuncs.com"
    assert config["allowedAgentModes"] == ["normal"]
    assert config["aliyunCLIForwardEnv"] == list(runner.FORWARDED_ENV)
    assert runtime.count(json.dumps(str(fake_cli.resolve()))) == 2

    runner._restore_skills(backups)

    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (destination / "config.json").exists()


def test_fake_cli_normalizes_captured_windows_newlines_before_writing() -> None:
    assert _fake_cli()._normalize_newlines("first\r\nsecond\r\n") == "first\nsecond\n"


@pytest.mark.skipif(
    not hasattr(os, "chflags") or not hasattr(stat, "UF_IMMUTABLE"),
    reason="user immutable flags are not supported",
)
def test_installed_skill_restores_user_immutable_files(tmp_path) -> None:
    runner = _runner()
    skill_root = tmp_path / "qoder-skills"
    destination = skill_root / "alicloud-ros-agent"
    destination.mkdir(parents=True)
    config = destination / "config.json"
    config.write_text('{"original":true}\n', encoding="utf-8")
    original_flags = config.stat().st_flags
    os.chflags(config, original_flags | stat.UF_IMMUTABLE, follow_symlinks=False)
    fake_cli = tmp_path / "fake-aliyun"
    fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")

    try:
        backups = runner._install_patched_skill(REPO_ROOT, [skill_root], fake_cli)
        assert json.loads(config.read_text(encoding="utf-8"))["transport"] == "aliyun_cli"

        runner._restore_skills(backups)

        assert json.loads(config.read_text(encoding="utf-8")) == {"original": True}
        assert config.stat().st_flags & stat.UF_IMMUTABLE
    finally:
        if config.exists():
            os.chflags(config, config.stat().st_flags & ~stat.UF_IMMUTABLE, follow_symlinks=False)


def _request(
    invocation: int,
    *,
    outcome: str,
    query: bool,
    session: bool,
    cursor_sequence: int | None,
    bootstrap_acked: bool = True,
) -> dict[str, object]:
    return {
        "operation": "start-chat",
        "invocation": invocation,
        "hasQuery": query,
        "hasSessionId": session,
        "sessionHash": "session" if session else None,
        "hasCursor": cursor_sequence is not None,
        "streamOptionsInBody": cursor_sequence is not None,
        "cursorIdentityHash": "stream" if cursor_sequence is not None else None,
        "cursorSequence": cursor_sequence,
        "action": "Reconnect" if cursor_sequence is not None else None,
        "endpointVerified": True,
        "profileApplied": True,
        "bootstrapAcked": bootstrap_acked,
        "outcome": outcome,
    }


@pytest.mark.parametrize(
    ("scenario", "requests", "final_text"),
    [
        ("normal", [_request(1, outcome="success", query=True, session=False, cursor_sequence=None)], "marker"),
        (
            "first-call-timeout",
            [
                _request(1, outcome="timeout", query=True, session=False, cursor_sequence=None),
                _request(2, outcome="success", query=False, session=True, cursor_sequence=1),
            ],
            "xxx1 xxx2 xxx3",
        ),
        (
            "reconnect-call-timeout",
            [
                _request(1, outcome="connection-reset", query=True, session=False, cursor_sequence=None),
                _request(2, outcome="timeout", query=False, session=True, cursor_sequence=1),
                _request(3, outcome="success", query=False, session=True, cursor_sequence=2),
            ],
            "xxx1 xxx2 xxx3",
        ),
    ],
)
def test_scenario_checks_cover_normal_first_timeout_and_reconnect_timeout(
    scenario, requests, final_text
) -> None:
    runner = _runner()

    checks = runner._scenario_checks(scenario, {"requests": requests}, final_text, "marker")

    assert checks
    assert all(checks.values())


def test_mcp_server_requires_reconnect_stream_options_in_body(tmp_path) -> None:
    server = _mcp_server()
    state_path = tmp_path / "state.json"
    server._start_request(
        state_path,
        [
            "ros",
            "start-chat",
            "--endpoint",
            "ros-pre.aliyuncs.com",
            "--query",
            "fixture",
            "--agent-version",
            "V2",
        ],
    )
    body = json.dumps(
        {"StreamOptions.Action": "Reconnect", "StreamOptions.Cursor": "v1.stream.3"},
        separators=(",", ":"),
    )
    _index, request = server._start_request(
        state_path,
        [
            "ros",
            "start-chat",
            "--endpoint",
            "ros-pre.aliyuncs.com",
            "--agent-version",
            "V2",
            "--session-id",
            "session-1",
            "--body",
            body,
        ],
    )

    assert request["streamOptionsInBody"] is True
    assert request["action"] == "Reconnect"
    assert request["cursorSequence"] == 3
    assert server._valid_start_shape(request) is True

    with pytest.raises(ValueError, match="must be sent in --body"):
        server._start_request(
            tmp_path / "legacy.json",
            [
                "ros",
                "start-chat",
                "--endpoint",
                "ros-pre.aliyuncs.com",
                "--session-id",
                "session-1",
                "--stream-options",
                "Action=Reconnect",
                "Cursor=v1.stream.3",
            ],
        )


def _real_cli_fixture(tmp_path: Path, *, sleep_seconds: float) -> Path:
    path = tmp_path / "real-aliyun-fixture.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "assert sys.argv[1:3] == ['ros', 'start-chat']\n"
        "assert sys.argv[sys.argv.index('--endpoint') + 1] == 'ros-pre.aliyuncs.com'\n"
        "assert sys.argv[sys.argv.index('--profile') + 1] == 'test-guima'\n"
        "print(json.dumps({'id':'v1.fixture.1','data':{'contextId':'session-fixture','value':'one'}}), flush=True)\n"
        f"time.sleep({sleep_seconds!r})\n"
        + "print(json.dumps({'id':'v1.fixture.2','data':{'contextId':'session-fixture','value':'two'}}), flush=True)\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _run_fake_cli(tmp_path: Path, *, timeout_seconds: float, fixture_sleep: float):
    ack = tmp_path / "ack.json"
    state = tmp_path / "state.json"
    real_cli = _real_cli_fixture(tmp_path, sleep_seconds=fixture_sleep)
    env = os.environ.copy()
    env.update(
        {
            "IAC_CODE_E2E_MCP_SERVER": str(MCP_SERVER_PATH),
            "IAC_CODE_E2E_REAL_ALIYUN": str(real_cli),
            "IAC_CODE_E2E_CLI_IDENTITY": "test-guima",
            "IAC_CODE_E2E_MCP_TIMEOUT_SECONDS": str(timeout_seconds),
            "IAC_CODE_E2E_SCENARIO": "first-call-timeout" if fixture_sleep > timeout_seconds else "normal",
            "IAC_CODE_E2E_SCENARIO_STATE": str(state),
            "IAC_CODE_E2E_MCP_STDERR_LOG": str(tmp_path / "mcp.log"),
            "IAC_CODE_E2E_PYTHON": sys.executable,
            "IAC_CODE_E2E_BOOTSTRAP_ACK_TIMEOUT_SECONDS": "5",
            "ALICLOUD_ROS_AGENT_INVOCATION_ID": "invocation-fixture",
            "ALICLOUD_ROS_AGENT_BOOTSTRAP_ACK_FILE": str(ack),
            "ALICLOUD_ROS_AGENT_BOOTSTRAP_PROTOCOL": "startchat-reconnect-bootstrap-v1",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(FAKE_CLI_PATH),
            "ros",
            "start-chat",
            "--endpoint",
            "ros-pre.aliyuncs.com",
            "--query",
            "fixture",
            "--agent-version",
            "V2",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdout is not None
    with ThreadPoolExecutor(max_workers=1) as pool:
        bootstrap_line = pool.submit(process.stdout.readline).result(timeout=15)
    bootstrap = json.loads(bootstrap_line)
    ack.write_text(
        json.dumps(
            {
                "invocationId": bootstrap["invocationId"],
                "eventId": bootstrap["event"]["id"],
                "committed": True,
            }
        ),
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=20)
    return process.returncode, bootstrap_line + stdout, stderr, json.loads(state.read_text(encoding="utf-8"))


def test_fake_cli_uses_real_stdio_mcp_progress_and_ack_before_returning_cli_output(tmp_path) -> None:
    return_code, stdout, stderr, state = _run_fake_cli(tmp_path, timeout_seconds=5, fixture_sleep=0)

    values = [json.loads(line) for line in stdout.splitlines()]
    assert return_code == 0
    assert stderr == ""
    assert values[0]["invocationId"] == "invocation-fixture"
    assert values[0]["event"]["id"] == "v1.fixture.1"
    assert [value["id"] for value in values[1:]] == ["v1.fixture.1", "v1.fixture.2"]
    assert state["requests"][0]["bootstrapAcked"] is True
    assert state["requests"][0]["outcome"] == "success"


def test_mcp_timeout_discards_ordinary_result_but_preserves_committed_bootstrap(tmp_path) -> None:
    return_code, stdout, stderr, state = _run_fake_cli(tmp_path, timeout_seconds=1, fixture_sleep=3)

    assert return_code == 1
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout)["event"]["id"] == "v1.fixture.1"
    assert json.loads(stderr)["code"] == "ExecutorTimeout"
    assert state["requests"][0]["bootstrapAcked"] is True
    assert state["requests"][0]["outcome"] == "timeout"
