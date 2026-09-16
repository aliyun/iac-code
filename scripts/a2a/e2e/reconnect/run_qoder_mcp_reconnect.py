#!/usr/bin/env python3
"""Credential-gated Qoder Work -> fake CLI -> MCP -> real CLI -> ROS pre E2E."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENDPOINT = "ros-pre.aliyuncs.com"
PROFILE = "test-guima"
MIN_ROS_PLUGIN_VERSION = (0, 9, 1)
BOOTSTRAP_CAPABILITY = "startchat-reconnect-bootstrap-v1"
MCP_TIMEOUT_SECONDS = 120.0
TERMINAL_STATES = {"turn-completed", "completed"}
FAILURE_STATES = {"failed", "canceled", "rejected"}
SCENARIOS = ("normal", "first-call-timeout", "reconnect-call-timeout")
FORWARDED_ENV = (
    "IAC_CODE_E2E_MCP_SERVER",
    "IAC_CODE_E2E_REAL_ALIYUN",
    "IAC_CODE_E2E_CLI_IDENTITY",
    "IAC_CODE_E2E_MCP_TIMEOUT_SECONDS",
    "IAC_CODE_E2E_SCENARIO",
    "IAC_CODE_E2E_SCENARIO_STATE",
    "IAC_CODE_E2E_MCP_STDERR_LOG",
    "IAC_CODE_E2E_PYTHON",
    "IAC_CODE_E2E_BOOTSTRAP_ACK_TIMEOUT_SECONDS",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--long-sleep-seconds", type=int, default=75)
    parser.add_argument("--max-qoder-turns", type=int, default=12)
    parser.add_argument("--qoder-turn-timeout", type=float, default=300.0)
    parser.add_argument(
        "--qoder-cli",
        type=Path,
        default=Path("/Applications/QoderWork.app/Contents/Resources/bin/qodercli"),
    )
    parser.add_argument("--qoder-config-dir", type=Path, default=Path("~/.qoderwork"))
    parser.add_argument("--real-aliyun", type=Path)
    parser.add_argument("--skill-root", type=Path, action="append", default=None)
    args = parser.parse_args()
    if args.skill_root is None:
        args.skill_root = [Path("~/.qoder/skills"), Path("~/.qoderwork/skills")]
    return args


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _plugin_version(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _manager_root(run_id: str) -> Path:
    return Path(tempfile.gettempdir()).resolve() / "iac-code-reconnect-e2e" / run_id


@dataclass(frozen=True)
class _SkillBackup:
    destination: Path
    backup_root: Path
    existed: bool


def _remove_path(path: Path) -> None:
    immutable = getattr(stat, "UF_IMMUTABLE", 0)
    chflags = getattr(os, "chflags", None)
    if immutable and chflags is not None and (path.exists() or path.is_symlink()):
        targets = [path]
        if path.is_dir() and not path.is_symlink():
            targets.extend(path.rglob("*"))
        for target in targets:
            try:
                flags = target.lstat().st_flags
            except FileNotFoundError:
                continue
            if flags & immutable:
                chflags(target, flags & ~immutable, follow_symlinks=False)
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _restore_skills(backups: list[_SkillBackup]) -> None:
    errors: list[tuple[_SkillBackup, OSError]] = []
    for backup in reversed(backups):
        restored = False
        try:
            _remove_path(backup.destination)
            saved = backup.backup_root / "skill"
            if backup.existed:
                shutil.copytree(saved, backup.destination, symlinks=True)
            restored = True
        except OSError as exc:
            errors.append((backup, exc))
        finally:
            if restored:
                _remove_path(backup.backup_root)
    if errors:
        failed, cause = errors[0]
        raise RuntimeError(
            "failed to restore Qoder Skill {}; backup retained at {}".format(
                failed.destination,
                failed.backup_root,
            )
        ) from cause


def _install_patched_skill(repo_root: Path, roots: list[Path], fake_cli: Path) -> list[_SkillBackup]:
    source = repo_root / "skills" / "alicloud-ros-agent"
    backups: list[_SkillBackup] = []
    try:
        destinations = []
        for raw_root in roots:
            destination = raw_root.expanduser().resolve() / "alicloud-ros-agent"
            existed = destination.exists() or destination.is_symlink()
            if existed and (destination.is_symlink() or not destination.is_dir()):
                raise RuntimeError("Qoder Skill destination must be a directory: {}".format(destination))
            backup_root = Path(tempfile.mkdtemp(prefix="iac-code-reconnect-skill-backup-"))
            if existed:
                shutil.copytree(destination, backup_root / "skill", symlinks=True)
            backups.append(_SkillBackup(destination, backup_root, existed))
            destinations.append(destination)

        default_expression = json.dumps(str(fake_cli.resolve()))
        needle = 'add_argument("--aliyun-path", default="aliyun")'
        replacement = 'add_argument("--aliyun-path", default={})'.format(default_expression)
        config = {
            "transport": "aliyun_cli",
            "aliyunCLIExecutionMode": "remote",
            "endpoint": ENDPOINT,
            "allowedAgentModes": ["normal"],
            "enableThinking": True,
            "managerIdleSeconds": 3,
            "aliyunCLIForwardEnv": list(FORWARDED_ENV),
        }
        for destination in destinations:
            _remove_path(destination)
            shutil.copytree(source, destination, symlinks=True)
            runtime = destination / "scripts" / "_ros_agent_runtime.py"
            text = runtime.read_text(encoding="utf-8")
            if text.count(needle) != 2:
                raise RuntimeError("the installed Skill no longer has the two expected aliyun-path defaults")
            runtime.write_text(text.replace(needle, replacement), encoding="utf-8")
            _write_json(destination / "config.json", config)
    except BaseException:
        _restore_skills(backups)
        raise
    return backups


def _plugin_manifest() -> dict[str, Any]:
    root = Path(os.environ.get("ALIBABA_CLOUD_CLI_PLUGIN_DIR", "~/.aliyun/plugins")).expanduser().resolve()
    path = root / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("the aliyun CLI plugin manifest is unavailable or invalid") from exc
    plugins = value.get("plugins") if isinstance(value, dict) else None
    plugin = plugins.get("aliyun-cli-ros") if isinstance(plugins, dict) else None
    if not isinstance(plugin, dict):
        raise RuntimeError("the ROS aliyun CLI plugin is not installed")
    return plugin


def _preflight(args: argparse.Namespace, repo_root: Path) -> tuple[Path, dict[str, Any]]:
    if not args.allow_real_cloud:
        raise SystemExit("Refusing to run Qoder/LLM/real-cloud E2E without --allow-real-cloud")
    if args.long_sleep_seconds < 61:
        raise SystemExit("--long-sleep-seconds must be at least 61 so the two sleeps exceed the 120-second MCP limit")
    if args.max_qoder_turns <= 0 or args.qoder_turn_timeout <= 0:
        raise SystemExit("Qoder turn limits must be positive")

    qoder_cli = args.qoder_cli.expanduser().resolve()
    if not qoder_cli.is_file():
        raise RuntimeError("Qoder Work CLI is unavailable")
    qoder_config = args.qoder_config_dir.expanduser().resolve()
    if not qoder_config.is_dir():
        raise RuntimeError("Qoder Work config directory is unavailable")
    real_aliyun = args.real_aliyun.expanduser().resolve() if args.real_aliyun else None
    if real_aliyun is None:
        discovered = shutil.which("aliyun")
        real_aliyun = Path(discovered).resolve() if discovered else None
    if real_aliyun is None or not real_aliyun.is_file():
        raise RuntimeError("the real aliyun CLI is unavailable")

    profile_check = subprocess.run(
        [str(real_aliyun), "configure", "get", "--profile", PROFILE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    if profile_check.returncode != 0:
        raise RuntimeError("the required aliyun CLI Profile {} is unavailable".format(PROFILE))

    manifest = _plugin_manifest()
    version = manifest.get("version")
    parsed_version = _plugin_version(version)
    if parsed_version is None or parsed_version < MIN_ROS_PLUGIN_VERSION:
        raise RuntimeError(
            "ROS CLI plugin {} is not reconnect-ready: version 0.9.1 or newer is required".format(
                version if isinstance(version, str) else "unknown"
            )
        )

    dependency_check = subprocess.run(
        [sys.executable, "-c", "import mcp"],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    if dependency_check.returncode != 0:
        raise RuntimeError("the repository Python environment does not provide the mcp package")
    return real_aliyun, {
        "qoderAvailable": True,
        "profileAvailable": True,
        "rosPluginVersion": version,
        "rosPluginReconnectReady": True,
        "rosPluginStreamOptionsBody": True,
    }


def _task_prompt(scenario: str, sleep_seconds: int, marker: str) -> str:
    if scenario == "normal":
        return (
            "这是 StartChat 断线重连 E2E 的正常场景。不要调用任何工具，只回复唯一文本 {}，不要添加其他内容。"
        ).format(marker)
    return (
        "这是 StartChat 断线重连 E2E 的长任务场景。只使用 bash 工具执行下面这一条命令，并等待它完成；"
        "不要调用云 API，不要请求确认，不要改变命令：\n"
        "echo xxx1; sleep {sleep}; echo xxx2; sleep {sleep}; echo xxx3\n"
        "命令完成后，回复包含 xxx1、xxx2、xxx3 的简短结果。"
    ).format(sleep=sleep_seconds)


def _jobs(state_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    results = []
    for path in sorted((state_root / "jobs").glob("*/job.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            results.append((path, value))
    return results


def _bridge_subcommands(block: dict[str, Any]) -> tuple[str, list[str]]:
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return "", []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return "", []
    subcommands = re.findall(r"ros_agent\.py[\"']?\s+(check|start|follow)\b", command)
    return command, subcommands


def _qoder_turn(
    *,
    args: argparse.Namespace,
    env: dict[str, str],
    workspace: Path,
    state_root: Path,
    session_id: str,
    prompt: str,
    turn: int,
    run_dir: Path,
) -> dict[str, Any]:
    driver_policy = (
        "You are driving one bounded alicloud-ros-agent reconnect E2E. Use the installed Skill. Prefix every bridge "
        "command with ALICLOUD_ROS_AGENT_STATE_DIR={state}. Run check exactly once before managed start. Run managed "
        "start exactly once with --prompt-file task-prompt.txt --mode normal; do not pass --endpoint, --profile, "
        "--aliyun-path, or --follow. Once a job exists, never start another job or resend its prompt; only follow "
        "that job from the newest public integer cursor until it reaches a boundary. Do not execute the task "
        "locally and do not replace the remote Skill."
    ).format(state=state_root)
    command = [
        str(args.qoder_cli.expanduser().resolve()),
        "-p",
        "--output-format",
        "stream-json",
        "--config-dir",
        str(args.qoder_config_dir.expanduser().resolve()),
        "--dangerously-skip-permissions",
        "--append-system-prompt",
        driver_policy,
        "--cwd",
        str(workspace),
    ]
    if turn:
        command.extend(["--resume", session_id])
    else:
        command.extend(["--session-id", session_id])
    command.append(prompt)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=args.qoder_turn_timeout,
    )
    bridge_commands = []
    bridge_subcommands = []
    result_codes: set[str] = set()
    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        message = item.get("message") if isinstance(item, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            serialized = json.dumps(block, ensure_ascii=False)
            if block.get("type") == "tool_use":
                command, subcommands = _bridge_subcommands(block)
                if subcommands:
                    bridge_commands.append(command)
                    bridge_subcommands.extend(subcommands)
            if block.get("type") == "tool_result":
                result_codes.update(re.findall(r'"code"\s*:\s*"([A-Za-z0-9_.-]{1,80})"', serialized))
    evidence = {
        "turn": turn,
        "returnCode": completed.returncode,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "stdoutBytes": len(completed.stdout.encode("utf-8")),
        "stderrBytes": len(completed.stderr.encode("utf-8")),
        "bridgeCommandCount": len(bridge_subcommands),
        "bridgeCheck": bridge_subcommands.count("check"),
        "bridgeStart": bridge_subcommands.count("start"),
        "bridgeFollow": bridge_subcommands.count("follow"),
        "passedAliyunPath": any("--aliyun-path" in value for value in bridge_commands),
        "resultCodes": sorted(result_codes),
    }
    _append_jsonl(run_dir / "qoder-turns.jsonl", evidence)
    if completed.returncode != 0:
        raise RuntimeError("Qoder turn {} failed; see qoder-turns.jsonl".format(turn))
    if evidence["passedAliyunPath"]:
        raise RuntimeError("Qoder bypassed the temporary installed-Skill fake CLI default")
    return evidence


def _scenario_checks(scenario: str, state: dict[str, Any], final_text: str, marker: str) -> dict[str, bool]:
    requests = [item for item in state.get("requests", []) if isinstance(item, dict)]
    starts = [item for item in requests if item.get("operation") == "start-chat"]
    initial = starts[:1]
    reconnects = starts[1:]
    query_requests = [item for item in starts if item.get("hasQuery") is True]
    session_hashes = {item.get("sessionHash") for item in reconnects if item.get("sessionHash")}
    cursor_identities = {item.get("cursorIdentityHash") for item in reconnects if item.get("cursorIdentityHash")}
    cursor_sequences = [item.get("cursorSequence") for item in reconnects]
    timeout_invocations = [item.get("invocation") for item in starts if item.get("outcome") == "timeout"]
    common = {
        "one initial Query was sent": len(query_requests) == 1 and bool(initial) and initial[0].get("hasQuery") is True,
        "initial request did not pre-generate SessionId or cursor": bool(initial)
        and initial[0].get("hasSessionId") is False
        and initial[0].get("hasCursor") is False
        and initial[0].get("streamOptionsInBody") is False,
        "all reconnects omitted Query": all(item.get("hasQuery") is False for item in reconnects),
        "all reconnects used SessionId, Reconnect, and full cursor": all(
            item.get("hasSessionId") is True
            and item.get("hasCursor") is True
            and item.get("streamOptionsInBody") is True
            and item.get("action") == "Reconnect"
            and isinstance(item.get("cursorSequence"), int)
            for item in reconnects
        ),
        "reconnect stayed in one Session and stream": len(session_hashes) <= 1 and len(cursor_identities) <= 1,
        "reconnect cursor never moved backwards": cursor_sequences == sorted(cursor_sequences),
        "MCP applied test-guima and ros-pre to every call": bool(requests)
        and all(item.get("profileApplied") is True and item.get("endpointVerified") is True for item in requests),
        "last StartChat invocation completed": bool(starts) and starts[-1].get("outcome") == "success",
        "expected task output completed": (
            marker in final_text
            if scenario == "normal"
            else all(value in final_text for value in ("xxx1", "xxx2", "xxx3"))
        ),
    }
    if scenario == "normal":
        common.update(
            {
                "normal path had no forced timeout or disconnect": bool(initial)
                and initial[0].get("outcome") == "success"
                and not reconnects
                and not timeout_invocations,
            }
        )
    elif scenario == "first-call-timeout":
        common.update(
            {
                "first call timed out only after bootstrap ack": bool(initial)
                and initial[0].get("outcome") == "timeout"
                and initial[0].get("bootstrapAcked") is True,
                "first-call timeout reconnected": bool(reconnects),
                "only the first call timed out": timeout_invocations == [1],
            }
        )
    else:
        common.update(
            {
                "first call disconnected only after bootstrap ack": bool(initial)
                and initial[0].get("outcome") == "connection-reset"
                and initial[0].get("bootstrapAcked") is True,
                "the non-initial call timed out": len(starts) >= 3 and starts[1].get("outcome") == "timeout",
                "only the second call timed out": timeout_invocations == [2],
                "later timeout continued from the latest anchor": len(starts) >= 3
                and starts[2].get("hasQuery") is False
                and isinstance(starts[1].get("cursorSequence"), int)
                and isinstance(starts[2].get("cursorSequence"), int)
                and starts[2].get("cursorSequence") >= starts[1].get("cursorSequence"),
            }
        )
    return common


def _cancel_nonterminal_job(repo_root: Path, state_root: Path, env: dict[str, str]) -> None:
    jobs = _jobs(state_root)
    if len(jobs) != 1 or jobs[0][1].get("state") in TERMINAL_STATES | FAILURE_STATES:
        return
    manager_path = state_root / "manager.json"
    try:
        manager = json.loads(manager_path.read_text(encoding="utf-8"))
        script = Path(manager["scriptPath"]).resolve()
        job_id = str(jobs[0][1]["jobId"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    if script.name != "ros_agent.py" or "alicloud-ros-agent" not in script.parts:
        return
    subprocess.run(
        [sys.executable, str(script), "cancel", "--job-id", job_id],
        cwd=repo_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=90,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[4]
    real_aliyun, preflight = _preflight(args, repo_root)
    run_dir = args.run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    run_id = "reconnect-{}-{}".format(args.scenario, uuid.uuid4().hex[:8])
    manager_root = _manager_root(run_id)
    workspace = manager_root / "qoder-workspace"
    state_root = manager_root / "ros-agent-state"
    workspace.mkdir(parents=True)
    state_root.mkdir()
    marker = "RECONNECT_E2E_{}".format(uuid.uuid4().hex[:12])
    (workspace / "task-prompt.txt").write_text(
        _task_prompt(args.scenario, args.long_sleep_seconds, marker),
        encoding="utf-8",
    )
    fake_cli_source = repo_root / "scripts" / "a2a" / "e2e" / "reconnect" / "fake_aliyun_cli.py"
    fake_cli = run_dir / "fake-aliyun"
    fake_text = fake_cli_source.read_text(encoding="utf-8")
    fake_cli.write_text("#!{}\n{}".format(sys.executable, fake_text.split("\n", 1)[1]), encoding="utf-8")
    fake_cli.chmod(0o700)
    mcp_server = repo_root / "scripts" / "a2a" / "e2e" / "reconnect" / "aliyun_cli_mcp_server.py"
    scenario_state = run_dir / "mcp-scenario-state.json"
    env = os.environ.copy()
    env.update(
        {
            "ALICLOUD_ROS_AGENT_STATE_DIR": str(state_root),
            "ALICLOUD_ROS_AGENT_EXECUTOR_VERSION": "qoder-mcp-e2e-v1",
            "ALICLOUD_ROS_AGENT_EXECUTOR_CAPABILITIES": BOOTSTRAP_CAPABILITY,
            "IAC_CODE_E2E_MCP_SERVER": str(mcp_server),
            "IAC_CODE_E2E_REAL_ALIYUN": str(real_aliyun),
            "IAC_CODE_E2E_CLI_IDENTITY": PROFILE,
            "IAC_CODE_E2E_MCP_TIMEOUT_SECONDS": str(MCP_TIMEOUT_SECONDS),
            "IAC_CODE_E2E_SCENARIO": args.scenario,
            "IAC_CODE_E2E_SCENARIO_STATE": str(scenario_state),
            "IAC_CODE_E2E_MCP_STDERR_LOG": str(run_dir / "mcp-stderr.log"),
            "IAC_CODE_E2E_PYTHON": sys.executable,
            "IAC_CODE_E2E_BOOTSTRAP_ACK_TIMEOUT_SECONDS": "15",
            "PYTHONUTF8": "1",
        }
    )
    backups: list[_SkillBackup] = []
    job: dict[str, Any] | None = None
    qoder_session = str(uuid.uuid4())
    try:
        backups = _install_patched_skill(repo_root, args.skill_root, fake_cli)
        next_prompt = "按系统指令开始本次测试：只执行一次 alicloud-ros-agent readiness check。"
        for turn in range(args.max_qoder_turns):
            evidence = _qoder_turn(
                args=args,
                env=env,
                workspace=workspace,
                state_root=state_root,
                session_id=qoder_session,
                prompt=next_prompt,
                turn=turn,
                run_dir=run_dir,
            )
            jobs = _jobs(state_root)
            if not jobs:
                if evidence["bridgeCheck"] != 1:
                    raise RuntimeError("Qoder did not perform the one required readiness check")
                next_prompt = (
                    "readiness check 已完成。现在只执行一次 managed start：使用 task-prompt.txt、--mode normal，"
                    "不要传 --follow，也不要重复 check。"
                )
                continue
            if len(jobs) != 1:
                raise RuntimeError("expected exactly one ROS Agent job")
            job = jobs[0][1]
            state = str(job.get("state") or "")
            if state in FAILURE_STATES:
                raise RuntimeError("ROS Agent job ended in {}".format(state))
            if state == "input-required":
                raise RuntimeError("the reconnect E2E task unexpectedly requested input")
            if state in TERMINAL_STATES:
                break
            next_prompt = "只对当前 job 从最新整数 cursor 调用一次 follow；不要 start、continue 或重发任务。"
        else:
            raise TimeoutError("Qoder turn limit reached before the ROS Agent job completed")

        assert job is not None
        mcp_state = json.loads(scenario_state.read_text(encoding="utf-8"))
        checks = _scenario_checks(args.scenario, mcp_state, str(job.get("finalText") or ""), marker)
        qoder_turns = [
            json.loads(line)
            for line in (run_dir / "qoder-turns.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        checks["Qoder used the patched installed Skill"] = all(
            item.get("passedAliyunPath") is False for item in qoder_turns
        )
        checks["Qoder ran check and start exactly once"] = (
            sum(int(item.get("bridgeCheck") or 0) for item in qoder_turns) == 1
            and sum(int(item.get("bridgeStart") or 0) for item in qoder_turns) == 1
        )
        result = {
            "schemaVersion": 1,
            "runId": run_id,
            "scenario": args.scenario,
            "endpoint": ENDPOINT,
            "profile": PROFILE,
            "mcpTimeoutSeconds": MCP_TIMEOUT_SECONDS,
            "preflight": preflight,
            "checks": checks,
            "passed": all(checks.values()),
        }
        _write_json(run_dir / "result.json", result)
        return result
    finally:
        try:
            _cancel_nonterminal_job(repo_root, state_root, env)
        except (OSError, subprocess.SubprocessError):
            pass
        _restore_skills(backups)
        time.sleep(3.5)
        shutil.rmtree(manager_root, ignore_errors=True)


def main() -> int:
    result = run(_parse_args())
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
