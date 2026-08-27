#!/usr/bin/env python3
"""Credential-gated real Qoder -> StartChat -> iac-code permission-wait E2E."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import yaml

CONFIG_FILES = (".credentials.yml", ".cloud-credentials.yml", "settings.yml")
TERMINAL_STATES = {"turn-completed", "completed"}
FAILURE_STATES = {"failed", "canceled"}
PROMPT_FILE = Path(__file__).with_name("permission_wait_start_chat_prompt.md")
DEFAULT_QODER_TURN_TIMEOUT_SECONDS = 900.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-cloud", action="store_true")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("normal", "pipeline"), required=True)
    parser.add_argument("--region", default="cn-hangzhou")
    parser.add_argument("--answer-delay-seconds", type=float, default=0.0)
    parser.add_argument("--restart-at-first-permission", action="store_true")
    parser.add_argument("--resident-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--sub-pipeline-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--timeout-grace-seconds", type=float, default=30.0)
    parser.add_argument("--max-qoder-turns", type=int, default=30)
    parser.add_argument("--qoder-turn-timeout", type=float, default=DEFAULT_QODER_TURN_TIMEOUT_SECONDS)
    parser.add_argument(
        "--qoder-cli",
        type=Path,
        default=Path("/Applications/QoderWork.app/Contents/Resources/bin/qodercli"),
    )
    parser.add_argument("--qoder-config-dir", type=Path, default=Path("~/.qoderwork"))
    parser.add_argument("--source-config-dir", type=Path, default=Path("~/.iac-code"))
    parser.add_argument(
        "--skill-root",
        type=Path,
        action="append",
        default=None,
    )
    args = parser.parse_args()
    if args.skill_root is None:
        args.skill_root = [Path("~/.qoder/skills"), Path("~/.qoderwork/skills")]
    return args


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _copy_config(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        source_path = source / name
        if not source_path.is_file():
            raise RuntimeError("required iac-code configuration is missing: {}".format(name))
        destination_path = destination / name
        shutil.copy2(source_path, destination_path)
        os.chmod(destination_path, 0o600)


def _refresh_source_cloud_credentials(source: Path) -> None:
    """Refresh OAuth-backed STS in the source config before taking an isolated copy.

    OAuth refresh tokens may rotate. Refreshing only an isolated copy can leave the
    configured source with the invalidated predecessor and make the next scenario
    fail before its first cloud request. The real-cloud opt-in therefore refreshes
    the caller-selected source in place, exactly as a normal iac-code cloud call
    would, and only then snapshots it for this run.
    """

    previous = os.environ.get("IAC_CODE_CONFIG_DIR")
    os.environ["IAC_CODE_CONFIG_DIR"] = str(source)
    try:
        from iac_code.services.providers.aliyun import AliyunCredentials

        credential = AliyunCredentials.load_from_iac_code_config()
        if credential is None:
            raise RuntimeError("Alibaba Cloud credentials are missing from the source config")
        if credential.mode == "OAuth":
            AliyunCredentials.refresh_oauth_if_needed(credential)
    finally:
        if previous is None:
            os.environ.pop("IAC_CODE_CONFIG_DIR", None)
        else:
            os.environ["IAC_CODE_CONFIG_DIR"] = previous


def _configure_isolated_permissions(config_dir: Path) -> None:
    """Auto-allow incidental tools while keeping cloud mutations interactive."""

    settings_path = config_dir / "settings.yml"
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    settings = dict(raw) if isinstance(raw, dict) else {}
    settings["permissions"] = {
        "mode": "default",
        "allow": [
            "read_file",
            "write_file",
            "edit_file",
            "list_files",
            "glob",
            "grep",
            "web_fetch",
            "read_memory",
            "write_memory",
            "task_list",
            "task_get",
            "task_stop",
            "agent",
            "skill",
            "aliyun_doc_search",
            "aliyun_api_doc",
            "ros_validate_template",
            "ros_get_template_parameter_constraints",
            "ros_preview_template",
            "ros_estimate_template_cost",
            "infraguard_scan",
            "ask_user_question",
            "show_architecture_diagram",
            "show_candidate_detail",
            "complete_step",
        ],
        # The host Qoder process may use Bash to drive the Skill, but iac-code
        # itself must not be able to route cloud mutations around tool-level
        # permission checks by invoking the native aliyun CLI through Bash.
        "deny": ["bash(*)"],
        "ask": [
            "aliyun_api",
            "ros_deploy",
            "ros_stack_group",
            "ros_template",
            "ros_template_scratch",
            "ros_diagnostic",
            "ros_resource_type_registration",
            "ros_tag",
            "ros_stack",
            "ros_stack_instances",
        ],
        "additional_directories": [],
        "audit": {
            "include_tool_input": False,
            "max_file_bytes": 10 * 1024 * 1024,
            "max_files": 5,
        },
    }
    settings_path.write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(settings_path, 0o600)


@dataclass(frozen=True)
class _SkillInstallationBackup:
    destination: Path
    temporary_root: Path
    existed: bool


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _restore_skill_installations(backups: list[_SkillInstallationBackup]) -> None:
    errors: list[tuple[_SkillInstallationBackup, OSError]] = []
    for backup in reversed(backups):
        restored = False
        try:
            _remove_path(backup.destination)
            saved = backup.temporary_root / "skill"
            if backup.existed:
                shutil.copytree(saved, backup.destination, symlinks=True)
            restored = True
        except OSError as exc:
            errors.append((backup, exc))
        finally:
            if restored:
                shutil.rmtree(backup.temporary_root, ignore_errors=True)
    if errors:
        failed, cause = errors[0]
        raise RuntimeError(
            "failed to restore Qoder Skill {}; backup retained at {}".format(
                failed.destination,
                failed.temporary_root,
            )
        ) from cause


def _sync_skill(
    repo_root: Path,
    roots: list[Path],
    endpoint: str,
    *,
    mode: str,
) -> list[_SkillInstallationBackup]:
    source = repo_root / "skills" / "alicloud-ros-agent"
    backups: list[_SkillInstallationBackup] = []
    config = {
        "endpoint": endpoint,
        "allowedAgentModes": [mode],
        "managerIdleSeconds": 60,
    }
    try:
        destinations: list[Path] = []
        for raw_root in roots:
            destination = raw_root.expanduser().resolve() / "alicloud-ros-agent"
            existed = destination.exists() or destination.is_symlink()
            if existed and (not destination.is_dir() or destination.is_symlink()):
                raise RuntimeError("Qoder Skill destination must be a directory: {}".format(destination))
            temporary_root = Path(tempfile.mkdtemp(prefix="iac-code-skill-backup-"))
            try:
                if existed:
                    shutil.copytree(destination, temporary_root / "skill", symlinks=True)
            except BaseException:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise
            backups.append(_SkillInstallationBackup(destination, temporary_root, existed))
            destinations.append(destination)

        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            for name in ("SKILL.md", "agents", "scripts"):
                source_path = source / name
                destination_path = destination / name
                _remove_path(destination_path)
                if source_path.is_dir():
                    shutil.copytree(source_path, destination_path, symlinks=True)
                else:
                    shutil.copy2(source_path, destination_path)
            _write_json(destination / "config.json", config)
    except BaseException:
        _restore_skill_installations(backups)
        raise
    return backups


def _prompt_section(name: str, replacements: dict[str, str]) -> str:
    text = PROMPT_FILE.read_text(encoding="utf-8")
    match = re.search(r"^## {}\s+```text\s+(.*?)\s+```".format(re.escape(name)), text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise RuntimeError("E2E prompt section is missing: {}".format(name))
    result = match.group(1)
    for key, value in replacements.items():
        result = result.replace("{" + key + "}", value)
    return result


@dataclass
class _Service:
    command: list[str]
    cwd: Path
    env: dict[str, str]
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[str] | None = None
    stdout_handle: Any = None
    stderr_handle: Any = None

    def start(self) -> None:
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self.stdout_handle = self.stdout_path.open("a", encoding="utf-8")
        self.stderr_handle = self.stderr_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.env,
            stdout=self.stdout_handle,
            stderr=self.stderr_handle,
            text=True,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    self.process.kill()
                self.process.wait(timeout=15)
        for handle in (self.stdout_handle, self.stderr_handle):
            if handle is not None and not handle.closed:
                handle.close()
        self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()


def _wait_a2a(port: int, service: _Service, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    url = "http://127.0.0.1:{}/.well-known/agent-card.json".format(port)
    while time.monotonic() < deadline:
        if service.process is not None and service.process.poll() is not None:
            raise RuntimeError("A2A server exited before readiness")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("A2A server readiness timed out")


def _wait_port(port: int, service: _Service, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service.process is not None and service.process.poll() is not None:
            raise RuntimeError("StartChat relay exited before readiness")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("StartChat relay readiness timed out")


def _a2a_config(
    path: Path,
    *,
    port: int,
    persistence: Path,
    artifacts: Path,
    resident_timeout_seconds: float = 300.0,
    sub_pipeline_timeout_seconds: float = 300.0,
    timeout_grace_seconds: float = 30.0,
) -> None:
    path.write_text(
        "\n".join(
            [
                "host: 127.0.0.1",
                "port: {}".format(port),
                "transport: http",
                "persistence_dir: {}".format(persistence),
                "artifact_dir: {}".format(artifacts),
                "auto_approve_permissions: false",
                "log_to_stdout: true",
                "idle_shutdown_seconds: 0",
                "permission_wait:",
                "  resident_timeout_seconds: {}".format(resident_timeout_seconds),
                "  sub_pipeline_timeout_seconds: {}".format(sub_pipeline_timeout_seconds),
                "  timeout_grace_seconds: {}".format(timeout_grace_seconds),
                "",
            ]
        ),
        encoding="utf-8",
    )


def _generate_certificate(run_dir: Path) -> tuple[Path, Path]:
    cert = run_dir / "runtime" / "relay.crt"
    key = run_dir / "runtime" / "relay.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key, 0o600)
    return cert, key


def _jobs(state_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    results: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((state_root / "jobs").glob("*/job.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            results.append((path, value))
    return results


def _run_qoder(
    *,
    args: argparse.Namespace,
    env: dict[str, str],
    workspace: Path,
    session_id: str,
    prompt: str,
    turn: int,
    resume: bool,
    run_dir: Path,
) -> dict[str, Any]:
    state_dir = str(env.get("ALICLOUD_ROS_AGENT_STATE_DIR") or "")
    driver_policy = (
        "You are driving one bounded ROS Agent E2E job. Execute at most one ros_agent.py bridge command in each "
        "Qoder turn, then stop and return its bounded result. Across this session execute readiness check at most "
        "once and managed start exactly once. After a job exists, never start another job; use only follow, continue, "
        "or respond for that job. Prefix every ros_agent.py command with ALICLOUD_ROS_AGENT_STATE_DIR={}. Do not "
        "replace the remote ROS Agent with local cloud, template, or deployment work."
    ).format(state_dir)
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
    if resume:
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
    stdout_bytes = len(completed.stdout.encode("utf-8"))
    stderr_bytes = len(completed.stderr.encode("utf-8"))
    assistant_text_blocks = 0
    assistant_mermaid = False
    content_block_index = 0
    first_mermaid_block_index: int | None = None
    first_cloud_permission_block_index: int | None = None
    bridge_command_count = 0
    bridge_managed_start = False
    bridge_managed_start_count = 0
    bridge_check_count = 0
    bridge_state_dir_bound = False
    bridge_state_dir_bound_count = 0
    bridge_start_shape_ok = False
    bridge_script_path_kinds: set[str] = set()
    bridge_tool_use_ids: set[str] = set()
    bridge_result_codes: set[str] = set()
    bridge_result_states: set[str] = set()
    bridge_result_ok_values: set[bool] = set()

    def contains_cloud_permission(value: Any) -> bool:
        if isinstance(value, dict):
            if (
                value.get("kind") == "permission"
                and value.get("effect") == "cloud_change"
                and value.get("isReadOnly") is False
            ):
                return True
            return any(contains_cloud_permission(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_cloud_permission(child) for child in value)
        if isinstance(value, str):
            return all(
                re.search(pattern, value) is not None
                for pattern in (
                    r'"kind"\s*:\s*"permission"',
                    r'"effect"\s*:\s*"cloud_change"',
                    r'"isReadOnly"\s*:\s*false',
                )
            )
        return False

    for line in completed.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            content_block_index += 1
            if block.get("type") == "tool_use":
                serialized_block = json.dumps(block, ensure_ascii=False)
                if "ros_agent.py" in serialized_block:
                    tool_use_id = block.get("id")
                    if isinstance(tool_use_id, str) and tool_use_id:
                        bridge_tool_use_ids.add(tool_use_id)
                    bridge_command_count += 1
                    if " start " in serialized_block:
                        bridge_managed_start = True
                        bridge_managed_start_count += 1
                        bridge_start_shape_ok = bridge_start_shape_ok or all(
                            token in serialized_block for token in ("--prompt-file", "--mode", "--follow")
                        )
                    if " check" in serialized_block:
                        bridge_check_count += 1
                    if "ALICLOUD_ROS_AGENT_STATE_DIR" in serialized_block:
                        bridge_state_dir_bound = True
                        bridge_state_dir_bound_count += 1
                    if "<absolute-bridge-path>" in serialized_block:
                        bridge_script_path_kinds.add("placeholder")
                    elif "/.qoderwork/skills/alicloud-ros-agent/" in serialized_block:
                        bridge_script_path_kinds.add("qoderwork")
                    elif "/.qoder/skills/alicloud-ros-agent/" in serialized_block:
                        bridge_script_path_kinds.add("qoder")
                    elif "/skills/alicloud-ros-agent/" in serialized_block:
                        bridge_script_path_kinds.add("repository")
                    else:
                        bridge_script_path_kinds.add("other")
            if block.get("type") == "tool_result" and block.get("tool_use_id") in bridge_tool_use_ids:
                serialized_result = json.dumps(block.get("content"), ensure_ascii=False)
                for code in re.findall(r'"code"\s*:\s*"([A-Za-z0-9_.-]{1,80})"', serialized_result):
                    bridge_result_codes.add(code)
                for state in re.findall(r'"state"\s*:\s*"([A-Za-z0-9_.-]{1,80})"', serialized_result):
                    bridge_result_states.add(state)
                for raw_ok in re.findall(r'"ok"\s*:\s*(true|false)', serialized_result, re.IGNORECASE):
                    bridge_result_ok_values.add(raw_ok.casefold() == "true")
            if item.get("type") == "assistant" and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    assistant_text_blocks += 1
                    contains_mermaid = re.search(r"```\s*mermaid\b", text, re.IGNORECASE) is not None
                    assistant_mermaid = assistant_mermaid or contains_mermaid
                    if contains_mermaid and first_mermaid_block_index is None:
                        first_mermaid_block_index = content_block_index
            if (
                block.get("type") == "tool_result"
                and first_cloud_permission_block_index is None
                and contains_cloud_permission(block.get("content"))
            ):
                first_cloud_permission_block_index = content_block_index
    evidence = {
        "turn": turn,
        "returnCode": completed.returncode,
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "stdoutBytes": stdout_bytes,
        "stderrBytes": stderr_bytes,
        "nonWhitespaceOutput": bool(completed.stdout.strip()),
        "assistantTextBlocks": assistant_text_blocks,
        "assistantMermaid": assistant_mermaid,
        "firstMermaidBlockIndex": first_mermaid_block_index,
        "firstCloudPermissionBlockIndex": first_cloud_permission_block_index,
        "mentionsFollow": " follow" in completed.stdout.casefold(),
        "bridgeCommandCount": bridge_command_count,
        "bridgeManagedStart": bridge_managed_start,
        "bridgeManagedStartCount": bridge_managed_start_count,
        "bridgeCheckCount": bridge_check_count,
        "bridgeStateDirBound": bridge_state_dir_bound,
        "bridgeStateDirBoundCount": bridge_state_dir_bound_count,
        "bridgeStartShapeOk": bridge_start_shape_ok,
        "bridgeScriptPathKinds": sorted(bridge_script_path_kinds),
        "bridgeResultCodes": sorted(bridge_result_codes),
        "bridgeResultStates": sorted(bridge_result_states),
        "bridgeResultOkValues": sorted(bridge_result_ok_values),
    }
    _append_jsonl(run_dir / "qoder-turns.jsonl", evidence)
    if completed.returncode != 0:
        raise RuntimeError("Qoder turn {} failed; see bounded qoder-turns.jsonl".format(turn))
    return evidence


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _permission_records(config_dir: Path, shared_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def load(root: Path) -> list[dict[str, Any]]:
        records = []
        for path in sorted(root.rglob("permission-waits/pwb_*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    return load(config_dir), load(shared_root)


def _read_only_cloud_execution_evidence(config_dir: Path) -> list[dict[str, Any]]:
    """Return bounded transcript proof for a read-only call and its ToolResult."""

    successful_results: set[str] = set()
    read_only_calls: dict[str, dict[str, Any]] = {}
    for path in config_dir.rglob("session.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "aliyun_api"
                    and isinstance(block.get("id"), str)
                    and isinstance(block.get("input"), dict)
                    and block["input"].get("action") == "DescribeVpcs"
                ):
                    read_only_calls[block["id"]] = {
                        "toolUseId": block["id"],
                        "product": block["input"].get("product"),
                        "action": block["input"].get("action"),
                        "source": "session_transcript",
                    }
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error") is False
                    and isinstance(block.get("tool_use_id"), str)
                ):
                    successful_results.add(block["tool_use_id"])
    evidence = [
        {**call, "resultPersisted": tool_use_id in successful_results} for tool_use_id, call in read_only_calls.items()
    ]
    return sorted(evidence, key=lambda item: (str(item.get("action")), str(item.get("toolUseId"))))


def _job_event_types(job_path: Path) -> list[str]:
    event_types: list[str] = []
    events_path = job_path.with_name("events.jsonl")
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return event_types
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in _walk(value):
            event_type = item.get("eventType")
            if isinstance(event_type, str) and event_type:
                event_types.append(event_type)
    return event_types


def _safe_permission_observation(
    *,
    job: dict[str, Any],
    config_dir: Path,
    shared_root: Path,
    observed_at: float,
) -> dict[str, Any]:
    current = job.get("inputRequired")
    current = current if isinstance(current, dict) else {}
    local, shared = _permission_records(config_dir, shared_root)
    input_id = current.get("inputId")
    local_match = next((record for record in local if record.get("inputId") == input_id), None)
    shared_match = next((record for record in shared if record.get("inputId") == input_id), None)
    return {
        "observedAt": observed_at,
        "inputId": input_id,
        "kind": current.get("kind"),
        "permissionClass": current.get("permissionClass"),
        "toolName": current.get("toolName"),
        "toolUseId": current.get("toolUseId"),
        "isReadOnly": current.get("isReadOnly"),
        "effect": current.get("effect"),
        "optionCount": len(current.get("options", [])) if isinstance(current.get("options"), list) else 0,
        "localCheckpoint": local_match is not None,
        "sharedCheckpoint": shared_match is not None,
        "checkpointPhase": local_match.get("phase") if local_match else None,
        "checkpointGeneration": local_match.get("generation") if local_match else None,
    }


def _validate_permission_scope(
    current: dict[str, Any],
    stack_name: str,
    agent_workspace: Path,
    *,
    allowed_stack_ids: set[str] | None = None,
) -> None:
    if current.get("isReadOnly") is not False:
        raise AssertionError("permission must explicitly identify a non-read-only operation")
    effect = current.get("effect")
    target = str(current.get("target") or "")
    if effect not in {"cloud_change", "file_change"}:
        raise AssertionError("permission effect is not an approved E2E mutation class")
    if not target:
        raise AssertionError("permission target must be non-empty")
    if effect == "cloud_change":
        allowed_targets = {stack_name, *(allowed_stack_ids or set())}
        identifier_characters = r"A-Za-z0-9_.:-"
        if not any(
            value
            and re.search(
                r"(?<![{}]){}(?![{}])".format(
                    identifier_characters,
                    re.escape(value),
                    identifier_characters,
                ),
                target,
            )
            for value in allowed_targets
        ):
            raise AssertionError("cloud permission target is outside the exact run-scoped stack")
    if effect == "file_change":
        candidate = (agent_workspace / target).resolve()
        try:
            candidate.relative_to(agent_workspace.resolve())
        except ValueError as exc:
            raise AssertionError("file permission target escapes the E2E workspace") from exc


def _answer_prompt(current: dict[str, Any]) -> str:
    kind = current.get("kind")
    if kind == "permission":
        return "允许当前明确展示且属于本次范围的非只读操作，仅允许一次，然后继续同一个 job。"
    if kind == "candidate_selection":
        return "选择推荐候选；若没有明确推荐，选择第一个候选。继续同一个 job。"
    if kind == "ask_user_question":
        return "选择当前问题中的第一个已有 VPC；如果这是部署或删除确认，则确认。继续同一个 job。"
    return "继续处理当前明确展示的输入，并保持同一个 ROS Agent job。"


def _aliyun_json(aliyun: str, arguments: list[str], *, timeout: float = 60.0) -> Any:
    completed = subprocess.run(
        [aliyun, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError("native aliyun CLI call failed: {}".format(" ".join(arguments[:3])))
    value = json.loads(completed.stdout)
    return value


def _objects_with(value: Any, key: str, expected: str) -> list[dict[str, Any]]:
    return [item for item in _walk(value) if item.get(key) == expected]


def _cloud_inventory(aliyun: str, region: str, stack_name: str, vswitch_name: str) -> dict[str, Any]:
    vpcs = _aliyun_json(aliyun, ["vpc", "DescribeVpcs", "--RegionId", region, "--PageSize", "50"])
    stacks = _aliyun_json(
        aliyun,
        ["ros", "ListStacks", "--RegionId", region, "--StackName.1", stack_name, "--PageSize", "50"],
    )
    vswitches = _aliyun_json(
        aliyun,
        ["vpc", "DescribeVSwitches", "--RegionId", region, "--VSwitchName", vswitch_name, "--PageSize", "50"],
    )
    return {
        "vpcs": sorted(
            {str(item["VpcId"]): str(item.get("Status") or "") for item in _walk(vpcs) if item.get("VpcId")}.items()
        ),
        "stacks": [
            {"stackId": item.get("StackId"), "stackName": item.get("StackName"), "status": item.get("Status")}
            for item in _objects_with(stacks, "StackName", stack_name)
        ],
        "vswitches": [
            {
                "vSwitchId": item.get("VSwitchId"),
                "vSwitchName": item.get("VSwitchName"),
                "vpcId": item.get("VpcId"),
                "status": item.get("Status"),
            }
            for item in _objects_with(vswitches, "VSwitchName", vswitch_name)
        ],
    }


def _cleanup_exact_stack(aliyun: str, region: str, inventory: dict[str, Any]) -> None:
    for stack in inventory.get("stacks", []):
        stack_id = stack.get("stackId")
        status = str(stack.get("status") or "")
        if not stack_id or status == "DELETE_COMPLETE":
            continue
        subprocess.run(
            [aliyun, "ros", "DeleteStack", "--RegionId", region, "--StackId", str(stack_id)],
            check=True,
            capture_output=True,
            timeout=60,
        )


def _cleanup_exact_vswitches(aliyun: str, region: str, inventory: dict[str, Any]) -> bool:
    succeeded = True
    for vswitch in inventory.get("vswitches", []):
        vswitch_id = vswitch.get("vSwitchId")
        if not vswitch_id:
            continue
        completed = subprocess.run(
            [aliyun, "vpc", "DeleteVSwitch", "--RegionId", region, "--VSwitchId", str(vswitch_id)],
            check=False,
            capture_output=True,
            timeout=60,
        )
        succeeded = completed.returncode == 0 and succeeded
    return succeeded


def _remove_sensitive_run_data(config_dir: Path) -> None:
    """Remove copied credentials and full session transcripts from E2E artifacts."""

    for name in (".credentials.yml", ".cloud-credentials.yml"):
        (config_dir / name).unlink(missing_ok=True)
    for path in config_dir.rglob("session.jsonl"):
        path.unlink(missing_ok=True)


def _architecture_preceded_deployment_permission(
    qoder_turns: list[dict[str, Any]],
    permission_observations: list[dict[str, Any]],
) -> bool:
    permission_turns = [
        int(item["qoderTurn"])
        for item in permission_observations
        if item.get("permissionClass") in {"normal", "pipeline"}
        and item.get("effect") == "cloud_change"
        and "qoderTurn" in item
    ]
    diagram_turns = [int(item["turn"]) for item in qoder_turns if item.get("assistantMermaid")]
    if not permission_turns or not diagram_turns:
        return False
    first_permission_turn = min(permission_turns)
    first_diagram_turn = min(diagram_turns)
    if first_diagram_turn < first_permission_turn:
        return True
    if first_diagram_turn > first_permission_turn:
        return False
    same_turn = next((item for item in qoder_turns if int(item.get("turn", -1)) == first_permission_turn), None)
    if same_turn is None:
        return False
    diagram_index = same_turn.get("firstMermaidBlockIndex")
    permission_index = same_turn.get("firstCloudPermissionBlockIndex")
    return isinstance(diagram_index, int) and isinstance(permission_index, int) and diagram_index < permission_index


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.allow_real_cloud:
        raise SystemExit("Refusing to run real Qoder/LLM/cloud E2E without --allow-real-cloud")
    if args.answer_delay_seconds < 0:
        raise SystemExit("--answer-delay-seconds must be non-negative")
    if args.resident_timeout_seconds <= 0 or args.sub_pipeline_timeout_seconds <= 0:
        raise SystemExit("permission resident and Sub Pipeline timeouts must be positive")
    if args.timeout_grace_seconds < 0:
        raise SystemExit("permission timeout grace must be non-negative")
    repo_root = Path(__file__).resolve().parents[4]
    run_dir = args.run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    config_dir = run_dir / "iac-code-config"
    shared_root = run_dir / "shared-backup"
    state_root = run_dir / "ros-agent-state"
    agent_workspace = run_dir / "agent-workspace"
    qoder_workspace = run_dir / "qoder-workspace"
    for path in (shared_root, state_root, agent_workspace, qoder_workspace, run_dir / "runtime", run_dir / "logs"):
        path.mkdir(parents=True, exist_ok=True)
    source_config_dir = args.source_config_dir.expanduser().resolve()

    aliyun = shutil.which("aliyun")
    if aliyun is None:
        raise RuntimeError("native aliyun CLI is unavailable")
    if not args.qoder_cli.expanduser().is_file():
        raise RuntimeError("Qoder CLI is unavailable")

    run_id = "pwait-{}-{}".format(args.mode, uuid.uuid4().hex[:8])
    stack_name = (run_id + "-stack")[:64]
    vswitch_name = (run_id + "-vsw")[:128]
    normal_port, pipeline_port, relay_port = _free_port(), _free_port(), _free_port()
    endpoint = "127.0.0.1:{}".format(relay_port)

    normal_config = run_dir / "runtime" / "a2a-normal.yml"
    pipeline_config = run_dir / "runtime" / "a2a-pipeline.yml"
    _a2a_config(
        normal_config,
        port=normal_port,
        persistence=run_dir / "runtime" / "a2a-normal-state",
        artifacts=run_dir / "artifacts" / "normal",
        resident_timeout_seconds=args.resident_timeout_seconds,
        sub_pipeline_timeout_seconds=args.sub_pipeline_timeout_seconds,
        timeout_grace_seconds=args.timeout_grace_seconds,
    )
    _a2a_config(
        pipeline_config,
        port=pipeline_port,
        persistence=run_dir / "runtime" / "a2a-pipeline-state",
        artifacts=run_dir / "artifacts" / "pipeline",
        resident_timeout_seconds=args.resident_timeout_seconds,
        sub_pipeline_timeout_seconds=args.sub_pipeline_timeout_seconds,
        timeout_grace_seconds=args.timeout_grace_seconds,
    )
    cert, key = _generate_certificate(run_dir)

    base_env = os.environ.copy()
    base_env.update(
        {
            "IAC_CODE_CONFIG_DIR": str(config_dir),
            "IAC_CODE_CONFIG_BACKUP_DIR": str(shared_root),
            "IACCODE_A2A_ALLOWED_CWDS": str(agent_workspace),
            "ALICLOUD_ROS_AGENT_STATE_DIR": str(state_root),
            "PYTHONPATH": os.pathsep.join(
                value for value in (str(repo_root / "src"), base_env.get("PYTHONPATH", "")) if value
            ),
            "PYTHONUTF8": "1",
        }
    )

    def a2a_service(mode: str, config_path: Path, port: int) -> _Service:
        env = dict(base_env)
        env["IAC_CODE_MODE"] = mode
        return _Service(
            command=[sys.executable, "-m", "iac_code.cli.main", "a2a", "--config", str(config_path)],
            cwd=repo_root,
            env=env,
            stdout_path=run_dir / "logs" / "a2a-{}.stdout.log".format(mode),
            stderr_path=run_dir / "logs" / "a2a-{}.stderr.log".format(mode),
        )

    normal = a2a_service("normal", normal_config, normal_port)
    pipeline = a2a_service("pipeline", pipeline_config, pipeline_port)
    relay = _Service(
        command=[
            sys.executable,
            str(repo_root / "tests" / "skill_bridge" / "start_chat_relay.py"),
            "--a2a-url",
            "http://127.0.0.1:{}".format(normal_port),
            "--pipeline-a2a-url",
            "http://127.0.0.1:{}".format(pipeline_port),
            "--workspace",
            str(agent_workspace),
            "--cert-file",
            str(cert),
            "--key-file",
            str(key),
            "--port",
            str(relay_port),
            "--metrics-file",
            str(run_dir / "relay-metrics.json"),
        ],
        cwd=repo_root,
        env=base_env,
        stdout_path=run_dir / "logs" / "relay.stdout.log",
        stderr_path=run_dir / "logs" / "relay.stderr.log",
    )
    selected_server = normal if args.mode == "normal" else pipeline
    before_inventory: dict[str, Any] | None = None
    deployed_inventory: dict[str, Any] | None = None
    after_inventory: dict[str, Any] | None = None
    permission_observations: list[dict[str, Any]] = []
    session_id = str(uuid.uuid4())
    first_permission_seen = False
    restart_performed = False
    cleanup_started = False
    cleanup_turn = -1
    deployment_confirmation_attempts = 0
    cleanup_confirmation_attempts = 0
    architecture_seen = False
    readiness_only_turn_seen = False
    job_path: Path | None = None
    skill_installation_backups: list[_SkillInstallationBackup] = []
    read_only_cloud_evidence: list[dict[str, Any]] = []
    next_prompt = _prompt_section(
        "Deployment",
        {
            "run_id": run_id,
            "stack_name": stack_name,
            "vswitch_name": vswitch_name,
            "mode": "Normal" if args.mode == "normal" else "Pipeline",
            "mode_arg": args.mode,
            "state_dir": str(state_root),
        },
    )
    try:
        _refresh_source_cloud_credentials(source_config_dir)
        _copy_config(source_config_dir, config_dir)
        _configure_isolated_permissions(config_dir)
        before_inventory = _cloud_inventory(aliyun, args.region, stack_name, vswitch_name)
        skill_installation_backups = _sync_skill(repo_root, args.skill_root, endpoint, mode=args.mode)
        normal.start()
        pipeline.start()
        _wait_a2a(normal_port, normal)
        _wait_a2a(pipeline_port, pipeline)
        relay.start()
        _wait_port(relay_port, relay)

        for turn in range(args.max_qoder_turns):
            qoder_evidence = _run_qoder(
                args=args,
                env=base_env,
                workspace=qoder_workspace,
                session_id=session_id,
                prompt=next_prompt,
                turn=turn,
                resume=turn > 0,
                run_dir=run_dir,
            )
            bridge_command_count = int(qoder_evidence.get("bridgeCommandCount") or 0)
            bridge_start_count = int(qoder_evidence.get("bridgeManagedStartCount") or 0)
            if bridge_start_count and qoder_evidence.get("bridgeStateDirBound") is not True:
                raise AssertionError("Qoder Skill bridge command did not bind the E2E state directory")
            architecture_seen = architecture_seen or bool(qoder_evidence.get("assistantMermaid"))
            jobs = _jobs(state_root)
            if not jobs:
                if bridge_command_count < 1:
                    raise AssertionError("Qoder did not execute a managed Skill bridge command")
                bridge_check_count = int(qoder_evidence.get("bridgeCheckCount") or 0)
                if readiness_only_turn_seen or bridge_check_count != bridge_command_count:
                    raise AssertionError("Qoder did not start the managed ROS Agent job after readiness")
                readiness_only_turn_seen = True
                next_prompt = (
                    "readiness check 已完成。现在必须且只执行一次 alicloud-ros-agent Skill 的 managed start，"
                    "使用精确的小写参数 --mode {} 开始之前给出的部署任务；不得再次 check，也不得在本地替代执行。"
                    "每条 bridge 命令必须显式设置 ALICLOUD_ROS_AGENT_STATE_DIR={}.".format(args.mode, state_root)
                )
                continue
            if len(jobs) != 1:
                raise AssertionError("expected exactly one ROS Agent job, found {}".format(len(jobs)))
            job_path, job = jobs[0]
            if job.get("mode") != args.mode:
                raise AssertionError(
                    "ROS Agent job mode {} does not match requested E2E mode {}".format(
                        job.get("mode"),
                        args.mode,
                    )
                )
            state = str(job.get("state") or "")
            if state in FAILURE_STATES:
                raise RuntimeError("ROS Agent job ended in {}".format(state))
            text_only_cleanup_summary = (
                cleanup_started
                and state in TERMINAL_STATES
                and int(job.get("turn") or 0) == cleanup_turn
                and int(qoder_evidence.get("assistantTextBlocks") or 0) > 0
            )
            if bridge_command_count < 1 and not text_only_cleanup_summary:
                raise AssertionError("Qoder did not execute a managed Skill bridge command")
            current = job.get("inputRequired")
            if state == "input-required" and isinstance(current, dict):
                input_id = current.get("inputId")
                if not any(item.get("inputId") == input_id for item in permission_observations):
                    observation = _safe_permission_observation(
                        job=job,
                        config_dir=config_dir,
                        shared_root=shared_root,
                        observed_at=time.time(),
                    )
                    observation["qoderTurn"] = turn
                    permission_observations.append(observation)
                    _append_jsonl(run_dir / "permission-observations.jsonl", observation)
                if current.get("kind") == "permission":
                    stack_ids = {
                        str(item.get("stackId"))
                        for item in (deployed_inventory or {}).get("stacks", [])
                        if item.get("stackId")
                    }
                    _validate_permission_scope(
                        current,
                        stack_name,
                        agent_workspace,
                        allowed_stack_ids=stack_ids,
                    )
                    is_target_permission = (
                        current.get("permissionClass") in {"normal", "pipeline"}
                        and current.get("effect") == "cloud_change"
                    )
                    if is_target_permission and not first_permission_seen:
                        first_permission_seen = True
                        if args.restart_at_first_permission:
                            selected_server.restart()
                            _wait_a2a(normal_port if args.mode == "normal" else pipeline_port, selected_server)
                            restart_performed = True
                        if args.answer_delay_seconds:
                            time.sleep(args.answer_delay_seconds)
                            observation = _safe_permission_observation(
                                job=_jobs(state_root)[0][1],
                                config_dir=config_dir,
                                shared_root=shared_root,
                                observed_at=time.time(),
                            )
                            observation["qoderTurn"] = turn
                            observation["afterDelaySeconds"] = args.answer_delay_seconds
                            permission_observations.append(observation)
                            _append_jsonl(run_dir / "permission-observations.jsonl", observation)
                next_prompt = _answer_prompt(current)
                continue
            if state in TERMINAL_STATES and not cleanup_started:
                current_inventory = _cloud_inventory(aliyun, args.region, stack_name, vswitch_name)
                active_stacks = [
                    item for item in current_inventory["stacks"] if item.get("status") != "DELETE_COMPLETE"
                ]
                if active_stacks and current_inventory["vswitches"]:
                    deployed_inventory = current_inventory
                    _write_json(run_dir / "deployed-inventory.json", current_inventory)
                    cleanup_started = True
                    cleanup_turn = int(job.get("turn") or 0)
                    next_prompt = _prompt_section(
                        "Cleanup",
                        {
                            "run_id": run_id,
                            "stack_name": stack_name,
                            "vswitch_name": vswitch_name,
                            "mode": args.mode,
                            "state_dir": str(state_root),
                        },
                    )
                else:
                    final_text = str(job.get("finalText") or "")
                    plan_ready = (
                        stack_name in final_text
                        and vswitch_name in final_text
                        and any(marker in final_text for marker in ("部署参数", "部署摘要", "模板校验", "CreateStack"))
                    )
                    if plan_ready and architecture_seen:
                        if deployment_confirmation_attempts >= 2:
                            raise RuntimeError("Qoder did not submit the explicit deployment confirmation")
                        deployment_confirmation_attempts += 1
                        next_prompt = _prompt_section(
                            "Confirm deployment",
                            {
                                "run_id": run_id,
                                "stack_name": stack_name,
                                "vswitch_name": vswitch_name,
                                "mode": args.mode,
                                "state_dir": str(state_root),
                            },
                        )
                    else:
                        next_prompt = _prompt_section(
                            "Continue deployment",
                            {
                                "run_id": run_id,
                                "stack_name": stack_name,
                                "vswitch_name": vswitch_name,
                                "mode": args.mode,
                                "state_dir": str(state_root),
                            },
                        )
                continue
            if state in TERMINAL_STATES and cleanup_started and int(job.get("turn") or 0) > cleanup_turn:
                break
            if state in TERMINAL_STATES and cleanup_started:
                if cleanup_confirmation_attempts >= 2:
                    raise RuntimeError("Qoder did not submit the explicit cleanup confirmation")
                cleanup_confirmation_attempts += 1
                next_prompt = _prompt_section(
                    "Confirm cleanup",
                    {
                        "run_id": run_id,
                        "stack_name": stack_name,
                        "vswitch_name": vswitch_name,
                        "mode": args.mode,
                        "state_dir": str(state_root),
                    },
                )
                continue
            next_prompt = "只对当前 job 调用 follow 继续观察，不要发送自然语言 continue 来催促远端。"
        else:
            raise TimeoutError("Qoder turn limit reached before cleanup completed")
    finally:
        finalization_error: BaseException | None = None
        for service in (relay, normal, pipeline):
            try:
                service.stop()
            except BaseException as exc:
                finalization_error = finalization_error or exc
        try:
            _restore_skill_installations(skill_installation_backups)
        except BaseException as exc:
            finalization_error = finalization_error or exc
        try:
            inventory = _cloud_inventory(aliyun, args.region, stack_name, vswitch_name)
            _cleanup_exact_stack(aliyun, args.region, inventory)
            deadline = time.monotonic() + 600
            last_vswitch_cleanup_attempt = 0.0
            while time.monotonic() < deadline:
                after_inventory = _cloud_inventory(aliyun, args.region, stack_name, vswitch_name)
                active = [item for item in after_inventory["stacks"] if item.get("status") != "DELETE_COMPLETE"]
                now = time.monotonic()
                if not active and after_inventory["vswitches"] and now - last_vswitch_cleanup_attempt >= 15:
                    # ROS can finish deleting a Stack while retaining a failed
                    # child resource. Inventory is exact-name scoped, so this
                    # removes only the VSwitch owned by the current E2E run.
                    # A transient dependency error while Stack deletion is
                    # converging must not abort polling; retry until the bounded
                    # cleanup deadline and let the final inventory prove removal.
                    _cleanup_exact_vswitches(aliyun, args.region, after_inventory)
                    last_vswitch_cleanup_attempt = now
                if not active and not after_inventory["vswitches"]:
                    break
                time.sleep(5)
        except Exception as exc:
            _write_json(run_dir / "cleanup-error.json", {"type": type(exc).__name__, "message": str(exc)[:500]})
        finally:
            read_only_cloud_evidence = _read_only_cloud_execution_evidence(config_dir)
            try:
                _write_json(run_dir / "read-only-cloud-evidence.json", read_only_cloud_evidence)
            finally:
                _remove_sensitive_run_data(config_dir)
        if finalization_error is not None:
            raise finalization_error

    before_vpcs = dict(before_inventory.get("vpcs", [])) if before_inventory else {}
    after_vpcs = dict(after_inventory.get("vpcs", [])) if after_inventory else {}
    relay_metrics = {}
    metrics_path = run_dir / "relay-metrics.json"
    if metrics_path.is_file():
        relay_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    relay_session_ids = {
        str(item.get("sessionId"))
        for item in relay_metrics.get("requests", [])
        if item.get("action") == "StartChat" and item.get("sessionId")
    }
    qoder_turns = []
    qoder_path = run_dir / "qoder-turns.jsonl"
    if qoder_path.is_file():
        qoder_turns = [json.loads(line) for line in qoder_path.read_text(encoding="utf-8").splitlines() if line]
    job_event_types = _job_event_types(job_path) if job_path is not None else []
    deploy_permission_turns = [
        int(item["qoderTurn"])
        for item in permission_observations
        if item.get("permissionClass") in {"normal", "pipeline"}
        and item.get("effect") == "cloud_change"
        and "qoderTurn" in item
    ]
    delayed_observations = [item for item in permission_observations if "afterDelaySeconds" in item]
    local_permission_records, shared_permission_records = _permission_records(config_dir, shared_root)
    prompted_tool_use_ids = {str(item.get("toolUseId")) for item in permission_observations if item.get("toolUseId")}
    checkpoint_tool_use_ids = {
        str(item.get("toolUseId"))
        for item in [*local_permission_records, *shared_permission_records]
        if item.get("toolUseId")
    }
    verified_read_only_calls = [
        item
        for item in read_only_cloud_evidence
        if item.get("action") == "DescribeVpcs"
        and item.get("resultPersisted") is True
        and item.get("toolUseId") not in prompted_tool_use_ids
        and item.get("toolUseId") not in checkpoint_tool_use_ids
    ]
    deployed_vpc_ids = {
        str(item.get("vpcId")) for item in (deployed_inventory or {}).get("vswitches", []) if item.get("vpcId")
    }
    delayed_phase_ok = True
    if args.answer_delay_seconds:
        delayed_phase_ok = bool(delayed_observations)
        if delayed_observations:
            delayed_phase = delayed_observations[-1].get("checkpointPhase")
            suspended_threshold = args.resident_timeout_seconds + args.timeout_grace_seconds + 5
            grace_threshold = args.resident_timeout_seconds + 5
            if args.answer_delay_seconds >= suspended_threshold:
                delayed_phase_ok = delayed_phase == "SUSPENDED"
            elif args.answer_delay_seconds >= grace_threshold:
                delayed_phase_ok = delayed_phase == "TIMEOUT_GRACE"
    checks = {
        "real non-read-only permission observed": any(
            item.get("kind") == "permission" and item.get("isReadOnly") is False for item in permission_observations
        ),
        "real cloud-change permission observed": bool(deploy_permission_turns),
        "no read-only permission observed": not any(item.get("isReadOnly") is True for item in permission_observations),
        "read-only DescribeVpcs executed without prompt or checkpoint": bool(verified_read_only_calls),
        "serial permission checkpoint existed": any(
            item.get("permissionClass") in {"normal", "pipeline"} and item.get("localCheckpoint")
            for item in permission_observations
        ),
        "serial permission shared commit existed": any(
            item.get("permissionClass") in {"normal", "pipeline"} and item.get("sharedCheckpoint")
            for item in permission_observations
        ),
        "sub pipeline created no durable checkpoint": not any(
            item.get("permissionClass") == "sub_pipeline" and item.get("localCheckpoint")
            for item in permission_observations
        ),
        "existing VPC inventory retained": bool(before_vpcs) and set(before_vpcs).issubset(after_vpcs),
        "VSwitch was deployed into a pre-existing VPC": bool(
            deployed_vpc_ids and deployed_vpc_ids.issubset(before_vpcs)
        ),
        "run stack and VSwitch existed before cleanup": bool(
            deployed_inventory
            and [item for item in deployed_inventory["stacks"] if item.get("status") != "DELETE_COMPLETE"]
            and deployed_inventory["vswitches"]
        ),
        "run stack cleaned": bool(
            after_inventory is not None
            and not [item for item in after_inventory["stacks"] if item.get("status") != "DELETE_COMPLETE"]
        ),
        "run VSwitch cleaned": bool(after_inventory is not None and not after_inventory["vswitches"]),
        "native StartChat relay was used": any(
            item.get("action") == "StartChat" for item in relay_metrics.get("requests", [])
        ),
        "all StartChat requests stayed in one ROS session": len(relay_session_ids) == 1,
        "Qoder emitted explanatory assistant text": sum(
            int(item.get("assistantTextBlocks") or 0) for item in qoder_turns
        )
        >= 3,
        "architecture diagram preceded deployment permission": _architecture_preceded_deployment_permission(
            qoder_turns,
            permission_observations,
        ),
        "Pipeline step progress was visible": args.mode != "pipeline"
        or ("step_started" in job_event_types and "step_completed" in job_event_types),
        "Pipeline candidate selection was visible": args.mode != "pipeline"
        or any(
            item.get("kind") == "candidate_selection" and int(item.get("optionCount") or 0) >= 2
            for item in permission_observations
        ),
        "Pipeline normal handoff was retained": args.mode != "pipeline"
        or bool(_jobs(state_root) and _jobs(state_root)[0][1].get("normalHandoffReady")),
        "configured delayed phase was observed": delayed_phase_ok,
        "requested A2A restart was performed": not args.restart_at_first_permission or restart_performed,
    }
    result = {
        "schemaVersion": 1,
        "runId": run_id,
        "mode": args.mode,
        "qoderSessionId": session_id,
        "answerDelaySeconds": args.answer_delay_seconds,
        "restartAtFirstPermission": args.restart_at_first_permission,
        "permissionWaitPolicy": [
            args.resident_timeout_seconds,
            args.sub_pipeline_timeout_seconds,
            args.timeout_grace_seconds,
        ],
        "checks": checks,
        "passed": all(checks.values()),
    }
    _write_json(run_dir / "result.json", result)
    return result


def main() -> int:
    result = run(_parse_args())
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
