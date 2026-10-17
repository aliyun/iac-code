#!/usr/bin/env python3
"""
Windows Compatibility Verification Script — A2A Mode
=====================================================

Starts the A2A HTTP server, then sends a "create VPC" prompt via
``iac-code a2a-client call`` to verify the full server <-> client flow.

Usage:
    python scripts/a2a/smoke/test_a2a_vpc.py

Prerequisites:
    - iac-code[a2a] installed (pip install -e ".[a2a]" or pip install iac-code[a2a])
    - LLM credentials configured
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

A2A_HOST = "127.0.0.1"
A2A_PORT = 41299
A2A_URL = f"http://{A2A_HOST}:{A2A_PORT}"
A2A_WORKSPACE = "."
TIMEOUT_SECONDS = 300


def wait_for_server(url: str, timeout: float = 30) -> bool:
    """Wait for the A2A HTTP service to become ready."""
    card_url = f"{url}/.well-known/agent-card.json"
    health_url = f"{url}/health"
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            req = urllib.request.Request(card_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True
                last_error = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            last_error = f"HTTP {e.code} {e.reason}"
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            if body:
                last_error += f" body={body}"
        except (urllib.error.URLError, OSError, ConnectionRefusedError) as e:
            last_error = str(e)
        time.sleep(1)

    print(f"{INFO} agent.json last error: {last_error}")
    # Try /health endpoint for comparison diagnostics
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"{INFO} /health returned: HTTP {resp.status}")
    except Exception as e:
        print(f"{INFO} /health also failed: {e}")
    return False


def _stream_stderr(proc: subprocess.Popen) -> None:
    """Background thread to print server stderr logs in real-time."""
    assert proc.stderr
    for line in proc.stderr:
        line = line.rstrip()
        if line:
            print(f"{INFO} [A2A Server] {line}")


def start_a2a_server(config_path: str) -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a",
        "--config", config_path,
        "--host", A2A_HOST,
        "--port", str(A2A_PORT),
    ]
    print(f"{INFO} Starting A2A server: {' '.join(cmd)}")
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    threading.Thread(target=_stream_stderr, args=(proc,), daemon=True).start()
    return proc


def _run_cli_call(
    prompt: str, *, stream: bool = False, cwd: str | None = None,
    context_id: str = '', timeout: float = TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a-client", "call",
        "--url", A2A_URL,
        "--prompt", prompt,
        "--cwd", cwd or A2A_WORKSPACE,
        "--timeout", str(timeout),
    ]
    if context_id:
        cmd.extend(['--context-id', context_id])
    if stream:
        cmd.append("--stream")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    print(f"{INFO} Running client: a2a-client call {'--stream ' if stream else ''}"
          + ('(native permission response)' if context_id else f'--prompt "{prompt[:30]}..."'))
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _template_permission_allowed(pending: dict, workspace: Path) -> bool:
    tool = pending.get('toolName')
    if pending.get('isReadOnly') is True and tool in {
        'read_file', 'glob', 'grep', 'ros_validate_template', 'ros_get_template_parameter_constraints',
    }:
        return True
    if tool not in {'write_file', 'edit_file'} or pending.get('effect') != 'file_change':
        return False
    target = pending.get('target')
    if (not isinstance(target, str) or not target.strip()
        or any(marker in target for marker in (' · ', '\n', '\x00', '<', '>', '...', '…'))):
        return False
    path = Path(target)
    if not path.is_absolute():
        path = workspace / path
    try:
        return path.resolve().is_relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False


def _permission_schema_supported(pending: dict) -> bool:
    # Native A2A Task metadata uses protobuf Struct; its numeric values are
    # represented as floats by MessageToDict. JSON booleans are not versions.
    version = pending.get('schemaVersion')
    return type(version) in (int, float) and version == 1


def run_a2a_client_call(prompt: str, *, stream: bool = False, cwd: str | None = None) -> subprocess.CompletedProcess:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    result = _run_cli_call(prompt, stream=stream, cwd=cwd, timeout=TIMEOUT_SECONDS)
    # The sync CLI returns at a native input boundary. That is a handshake,
    # not a finished template response. Resolve only this isolated task's
    # authorized template operations, within the original total deadline.
    if stream or result.returncode:
        return result
    seen: set[str] = set()
    context_id = None
    response = None
    state = 'unknown'
    same_input = False
    confirmation = 'not_sent'
    task = None

    def finish(reason: str) -> subprocess.CompletedProcess:
        result.smoke_permission_response_count = len(seen)
        result.smoke_sync_diagnostics = {
            'smoke_sync_exit_reason': reason,
            'smoke_sync_task_state': state,
            'smoke_sync_permission_response_count': len(seen),
            'smoke_sync_permission_confirmation': confirmation,
            'smoke_sync_same_permission_pending': same_input,
        }
        if reason != 'completed':
            if isinstance(task, dict):
                result.smoke_sync_diagnostics.update(_sync_failure_diagnostics(result.stdout, task=task))
            result.returncode = result.returncode or 1
            result.stdout = ''  # Never validate a permission receipt as a template.
        print(f'{INFO} Sync task outcome: {json.dumps(result.smoke_sync_diagnostics, sort_keys=True)}')
        return result

    def read(command: str, *options: str) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('task probe', TIMEOUT_SECONDS)
        return _read_task_diagnostic(command, *options, timeout=min(5, remaining))

    try:
        tasks = read('task-list', '--output', 'json', '--page-size', '2').get('tasks')
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            return finish('task_ambiguous')
        task_id = tasks[0].get('id')
        if not isinstance(task_id, str) or not task_id:
            return finish('invalid_task')
        while time.monotonic() < deadline:
            raw = read('task-get', '--task-id', task_id, '--history-length', '50')
            task = raw.get('task', raw)
            if (not isinstance(task, dict) or task.get('id') != task_id
                or not isinstance(task.get('status'), dict)):
                return finish('invalid_task')
            status = task.get('status', {})
            state = str(status.get('state', '')).lower().replace('_', '-').removeprefix('task-state-')
            if state not in {'submitted', 'working', 'input-required', 'completed',
                             'failed', 'canceled', 'rejected', 'auth-required'}:
                state = 'unknown'
                return finish('invalid_task')
            context = task.get('contextId')
            if not isinstance(context, str) or not context or context_id not in (None, context):
                return finish('invalid_task')
            context_id = context
            metadata = task.get('metadata')
            iac = metadata.get('iac_code') if isinstance(metadata, dict) else None
            final = iac.get('assistantFinal') if isinstance(iac, dict) else None
            turn_completed = (state == 'input-required' and isinstance(final, dict)
                              and final.get('complete') is True and not isinstance(iac.get('input'), dict)
                              and not iac.get('pendingPermissions'))
            ack = iac.get('permissionAck') if isinstance(iac, dict) else None
            if (response is not None and isinstance(ack, dict) and ack.get('accepted') is True
                and all(ack.get(key) == response[key] for key in ('inputId', 'toolUseId', 'decision'))):
                confirmation = 'acknowledged'
            if (state in {'working', 'completed'} or turn_completed) and confirmation == 'unconfirmed':
                confirmation = 'task_advanced'
            same_input = False
            if state == 'completed' or turn_completed:
                result.stdout = _completed_task_text(task)
                return finish('completed')
            if state not in {'submitted', 'working', 'input-required'}:
                return finish('task_ended')
            if state in {'submitted', 'working'}:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            pending = iac.get('input') if isinstance(iac, dict) else None
            if pending is None:
                # Normal mode ends a turn in INPUT_REQUIRED. Its final marker
                # can be replaced by later SDK metadata; require the same task's
                # released natural-completion receipt, never just missing input.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return finish('timeout')
                if _normal_turn_completed(task, timeout=min(5, remaining)):
                    if confirmation == 'unconfirmed':
                        confirmation = 'task_advanced'
                    final_text = _normal_turn_output(task, result.stdout, permission_submitted=bool(seen))
                    if not final_text:
                        return finish('final_output_unavailable')
                    result.stdout = final_text
                    return finish('completed')
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            if (not isinstance(pending, dict) or pending.get('kind') != 'permission'
                or not _permission_schema_supported(pending)
                or pending.get('requestTaskId') != task_id
                or pending.get('contextId') != context
                or not all(isinstance(pending.get(k), str) and pending[k] for k in ('inputId', 'toolUseId'))):
                return finish('unsupported_input')
            if not _template_permission_allowed(pending, Path(cwd or A2A_WORKSPACE)):
                return finish('unauthorized_permission')
            if pending['inputId'] in seen:
                same_input = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return finish('timeout')
                if _normal_turn_completed(task, timeout=min(5, remaining)):
                    confirmation = 'task_advanced'
                    final_text = _normal_turn_output(task, result.stdout, permission_submitted=True)
                    if not final_text:
                        return finish('final_output_unavailable')
                    result.stdout = final_text
                    return finish('completed')
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            response = {key: pending[key] for key in ('schemaVersion', 'kind', 'requestTaskId',
                                                     'contextId', 'inputId', 'toolUseId')}
            response['decision'] = 'allow_once'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish('timeout')
            seen.add(pending['inputId'])
            confirmation = 'unconfirmed'
            result = _run_cli_call('IAC_CODE_PERMISSION:' + json.dumps(response), cwd=cwd,
                                   context_id=context, timeout=remaining)
            if result.returncode:
                return finish('client_error')
    except subprocess.TimeoutExpired:
        return finish('timeout')
    except (OSError, ValueError):
        return finish('task_query_failed')
    return finish('timeout')


def _completed_task_text(task: dict) -> str:
    """Read this task's final agent reply, never an earlier turn or a file artifact."""
    history = task.get('history')
    for entry in reversed(history if isinstance(history, list) else []):
        if not isinstance(entry, dict) or entry.get('role') not in {'ROLE_AGENT', 'agent'}:
            break
        parts = entry.get('parts')
        text = (''.join(part['text'] for part in parts
                        if isinstance(part, dict) and isinstance(part.get('text'), str))
                if isinstance(parts, list) else '')
        if not text:
            break
        # The executor publishes the whole final reply after text deltas.
        return text
    message = task['status'].get('message')
    parts = message.get('parts') if isinstance(message, dict) else None
    return ''.join(part['text'] for part in parts
                   if isinstance(part, dict) and isinstance(part.get('text'), str)) if isinstance(parts, list) else ''


def _normal_turn_completed(task: dict, *, timeout: float) -> bool:
    query = urllib.parse.urlencode({'contextId': task['contextId']})
    with urllib.request.urlopen(f'{A2A_URL}/iac-code/execution/state?{query}', timeout=timeout) as response:
        control = json.loads(response.read().decode('utf-8'))
    return (isinstance(control, dict) and control.get('contextId') == task['contextId']
            and control.get('taskId') == task['id'] and control.get('phase') == 'terminated'
            and control.get('terminationReason') == 'natural_completion' and control.get('releaseReady') is True)


def _normal_turn_output(task: dict, cli_text: str, *, permission_submitted: bool) -> str:
    # A reply consumer may retain an old permission Task even after its owner
    # finished. After the matching completion proof, accept only a whole ROS
    # template from history or the just-finished CLI call, never a receipt.
    if not permission_submitted:
        # No reply/receipt call replaced the original sync output. Once that
        # task has released naturally, keep its own CLI reply for acceptance.
        return cli_text or _completed_task_text(task)
    for text in (_completed_task_text(task), cli_text):
        wire = text.strip()
        lines = wire.splitlines()
        if len(lines) >= 3 and lines[0] in {'```', '```json'} and lines[-1] == '```':
            wire = '\n'.join(lines[1:-1])
        try:
            value = json.loads(wire)
        except ValueError:
            continue
        resources = value.get('Resources') if isinstance(value, dict) else None
        if isinstance(resources, dict) and any(
            isinstance(resource, dict) and resource.get('Type') == 'ALIYUN::ECS::VPC'
            for resource in resources.values()
        ):
            return text
    return ''


def run_a2a_client_discover() -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, "-m", "iac_code.cli.main",
        "a2a-client", "discover",
        "--url", A2A_URL,
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    print(f"{INFO} Running client: a2a-client discover")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_discover(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} Step 1: Agent Card Discovery")
    result = run_a2a_client_discover()

    if result.returncode != 0:
        print(f"{FAIL} discover exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["discover succeeded"] = False
        return False

    stdout = result.stdout.strip()
    try:
        card = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{FAIL} discover output is not JSON: {stdout[:200]}")
        checks["discover succeeded"] = False
        return False

    checks["discover succeeded"] = True
    name = card.get("name", "")
    print(f"{INFO} Agent name: {name}")
    checks["Agent Card name is iac-code"] = name == "iac-code"
    return True


def _read_task_diagnostic(command: str, *options: str, timeout: float = 5) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "iac_code.cli.main", "a2a-client", command, "--url", A2A_URL, *options],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode != 0:
        raise ValueError("task diagnostic unavailable")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise ValueError("task diagnostic malformed")
    return payload["result"]


def _sync_failure_diagnostics(stdout: str, *, task: dict | None = None) -> dict:
    """Read the existing task only; publish counts and closed categories, never response text."""
    diagnostics = {
        "smoke_sync_output_length": len(stdout),
        "smoke_sync_permission_hint": any(word in stdout.lower() for word in ("permission", "权限", "允许", "授权")),
        "smoke_sync_task_probe_category": "query_failed",
    }
    try:
        if task is None:
            tasks = _read_task_diagnostic("task-list", "--output", "json", "--page-size", "2").get("tasks")
            if not isinstance(tasks, list):
                return diagnostics
            diagnostics["smoke_sync_task_count"] = len(tasks)
            if len(tasks) != 1:
                diagnostics["smoke_sync_task_probe_category"] = "task_ambiguous"
                return diagnostics
            task_id = tasks[0].get("id") if isinstance(tasks[0], dict) else None
            if not isinstance(task_id, str) or not task_id:
                return diagnostics
            result = _read_task_diagnostic("task-get", "--task-id", task_id, "--history-length", "50")
            task = result.get("task", result)
        else:
            diagnostics["smoke_sync_task_count"] = 1
            task_id = task.get('id')
        status = task.get("status") if isinstance(task, dict) else None
        if not isinstance(status, dict):
            return diagnostics
        raw_state = status.get("state")
        state = (
            raw_state.lower().replace("_", "-").removeprefix("task-state-")
            if isinstance(raw_state, str) else "unknown"
        )
        states = {
            "submitted", "working", "input-required", "completed", "failed", "canceled", "rejected", "auth-required",
        }
        diagnostics["smoke_sync_task_state"] = state if state in states else "unknown"
        message = status.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
        pending_inputs = []
        metadata = task.get('metadata')
        iac = metadata.get('iac_code') if isinstance(metadata, dict) else None
        if isinstance(iac, dict) and isinstance(iac.get('input'), dict):
            pending_inputs.append(iac['input'])
        if isinstance(parts, list):
            for part in parts:
                data = part.get("data") if isinstance(part, dict) else None
                if not isinstance(data, dict):
                    continue
                pending = data.get("input") if isinstance(data.get("input"), dict) else data
                pending_inputs.append(pending)
        metadata_pending = iac.get('input') if isinstance(iac, dict) else None
        diagnostics["smoke_sync_permission_metadata_present"] = isinstance(metadata_pending, dict)
        for pending in pending_inputs:
            if pending.get('kind') == 'permission':
                diagnostics["smoke_sync_permission_schema_supported"] = (
                    _permission_schema_supported(pending)
                )
                diagnostics["smoke_sync_permission_task_matches"] = pending.get('requestTaskId') == task_id
                context = task.get('contextId')
                diagnostics["smoke_sync_permission_context_matches"] = (
                    isinstance(context, str) and bool(context) and pending.get('contextId') == context
                )
                diagnostics["smoke_sync_permission_identity_complete"] = all(
                    isinstance(pending.get(key), str) and bool(pending[key]) for key in ('inputId', 'toolUseId')
                )
                diagnostics["smoke_sync_permission_workspace_allowed"] = _template_permission_allowed(
                    pending, Path(A2A_WORKSPACE)
                )
                target = pending.get('target')
                shape = ('missing' if not isinstance(target, str) or not target.strip() else
                         'multiple' if ' · ' in target else
                         'opaque' if any(marker in target for marker in ('\n', '\x00', '<', '>', '...', '…')) else
                         'absolute' if Path(target).is_absolute() else 'relative')
                diagnostics["smoke_sync_permission_target_shape"] = shape
                effect = pending.get('effect')
                diagnostics["smoke_sync_permission_effect"] = (
                    effect if isinstance(effect, str) and effect in
                    {'read', 'file_change', 'cloud_change', 'local_execution', 'unknown'}
                    else 'other'
                )
            kind = pending.get("kind")
            if isinstance(kind, str) and kind in {
                "permission", "ask_user_question", "candidate_selection", "deployment_confirmation",
            }:
                diagnostics["smoke_sync_pending_kind"] = kind
            tool = pending.get("toolName")
            if isinstance(tool, str) and tool in {
                "read_file", "write_file", "edit_file", "bash", "ask_user_question", "aliyun_api",
                "ros_validate_template", "ros_preview_template", "ros_estimate_template_cost", "ros_deploy",
            }:
                diagnostics["smoke_sync_pending_tool"] = tool
            if type(pending.get("isReadOnly")) is bool:
                diagnostics["smoke_sync_pending_read_only"] = pending["isReadOnly"]
        status_text = "".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)) \
            if isinstance(parts, list) else ""
        diagnostics["smoke_sync_status_text_matches_stdout"] = (
            bool(status_text) and status_text.strip() == stdout.strip()
        )
        history = task.get("history")
        pieces = []
        if isinstance(history, list):
            for entry in reversed(history):
                if not isinstance(entry, dict) or entry.get("role") not in {"ROLE_AGENT", "agent"}:
                    break
                parts = entry.get("parts")
                text = "".join(p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)) \
                    if isinstance(parts, list) else ""
                if not text:
                    break
                pieces.append(text)
        diagnostics["smoke_sync_trailing_agent_message_count"] = len(pieces)
        diagnostics["smoke_sync_history_vpc_marker"] = any(
            word in "".join(reversed(pieces)).upper() for word in ("VPC", "TEMPLATE", "CIDR", "ROSTEMPLATE")
        )
        diagnostics["smoke_sync_task_probe_category"] = "verified"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return diagnostics


def test_call_sync(checks: dict[str, bool], diagnostics: dict | None = None) -> bool:
    print(f"\n{INFO} Step 2: Synchronous call (create VPC)")
    prompt = _template_prompt()
    result = run_a2a_client_call(prompt)
    if diagnostics is not None:
        diagnostics.update(getattr(result, 'smoke_sync_diagnostics', {}))
        count = getattr(result, 'smoke_permission_response_count', 0)
        if type(count) is int and count > 0:
            diagnostics['smoke_sync_permission_response_count'] = count

    if result.returncode != 0:
        print(f"{FAIL} call exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["sync call succeeded"] = False
        checks["output contains VPC-related content"] = False
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} call output is empty")
        checks["sync call succeeded"] = False
        return False

    checks["sync call succeeded"] = True
    print(f"{INFO} Output length: {len(stdout)} chars")
    checks["output contains VPC-related content"] = any(
        kw in stdout.upper() for kw in ["VPC", "TEMPLATE", "CIDR", "ROSTEMPLATE"]
    )
    if diagnostics is not None and not checks["output contains VPC-related content"]:
        diagnostics.update(_sync_failure_diagnostics(stdout))
    return True


def test_call_stream(checks: dict[str, bool]) -> bool:
    print(f"\n{INFO} Step 3: Streaming call --stream (create VPC)")
    prompt = _template_prompt()
    result = run_a2a_client_call(prompt, stream=True)

    if result.returncode != 0:
        print(f"{FAIL} stream call exited with non-zero code: {result.returncode}")
        if result.stderr:
            print(f"{INFO} stderr:\n{result.stderr[:3000]}")
        checks["stream call succeeded"] = False
        return False

    stdout = result.stdout.strip()
    if not stdout:
        print(f"{FAIL} stream call output is empty")
        checks["stream call succeeded"] = False
        return False

    checks["stream call succeeded"] = True
    lines = stdout.split("\n")
    print(f"{INFO} Received {len(lines)} lines of streaming output")

    combined = stdout.upper()
    checks["stream output contains VPC-related content"] = any(
        kw in combined for kw in ["VPC", "TEMPLATE", "CIDR"]
    )
    return True


def _template_prompt() -> str:
    return ("帮我生成一个创建VPC的ROS模板，VPC名称为test-vpc，CIDR为172.16.0.0/12。"
            "最终直接回复完整JSON模板正文，不要仅回复文件路径或授权回执，不创建或部署云资源。"
            "无需保存文件；如需保存，只允许写入本用例隔离工作目录内，禁止写到目录外。"
            f"本用例隔离工作目录：{Path(A2A_WORKSPACE).resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    global A2A_PORT, A2A_URL, A2A_WORKSPACE
    if args.run_dir is not None:
        args.run_dir.mkdir(parents=True, exist_ok=True)
        workspace = args.run_dir / "workspace"
        workspace.mkdir(exist_ok=True)
        A2A_WORKSPACE = str(workspace)
    with socket.socket() as port_socket:
        port_socket.bind((A2A_HOST, 0))
        A2A_PORT = port_socket.getsockname()[1]
    A2A_URL = f"http://{A2A_HOST}:{A2A_PORT}"
    print("=" * 60)
    print("  iac-code A2A Mode Windows Compatibility Test")
    print("=" * 60)

    # A template-only smoke must not auto-approve cloud tool use.
    config_content = "auto-approve-permissions: false\n"
    config_fd, config_path = tempfile.mkstemp(suffix=".yml", prefix="a2a_test_")
    os.write(config_fd, config_content.encode("utf-8"))
    os.close(config_fd)

    server_proc = None
    checks: dict[str, bool] = {}
    diagnostics: dict = {}

    try:
        # Start A2A server
        server_proc = start_a2a_server(config_path)
        time.sleep(2)

        if server_proc.poll() is not None:
            print(f"{FAIL} A2A server exited immediately after start, exit code: {server_proc.returncode}")
            stderr = server_proc.stderr.read() if server_proc.stderr else ""
            if stderr:
                print(f"{INFO} stderr: {stderr[:500]}")
            checks["A2A server started"] = False
        else:
            print(f"{INFO} A2A server PID: {server_proc.pid}")
            print(f"{INFO} Waiting for server to become ready...")

            if not wait_for_server(A2A_URL, timeout=30):
                print(f"{FAIL} A2A server not ready within 30s")
                checks["A2A server started"] = True
                checks["A2A server ready"] = False
            else:
                checks["A2A server started"] = True
                checks["A2A server ready"] = True
                print(f"{PASS} A2A server is ready ({A2A_URL})")

                test_discover(checks)
                test_call_sync(checks, diagnostics)
                test_call_stream(checks)

    except Exception as e:
        print(f"{FAIL} Exception: {e}")
        checks["test execution"] = False
        import traceback
        traceback.print_exc()
    finally:
        # Stop A2A server
        if server_proc:
            print(f"\n{INFO} Stopping A2A server...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()

        # Clean up config file
        try:
            os.unlink(config_path)
        except OSError:
            pass

    # Summary
    print(f"\n{'=' * 60}")
    print("  Test Results Summary")
    print("=" * 60)
    all_pass = bool(checks)
    for desc, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print(f"{PASS} All A2A tests passed!")
    else:
        print(f"{FAIL} Some tests failed, check output above")

    if args.run_dir is not None:
        (args.run_dir / "summary.json").write_text(
            json.dumps(
                {"passed": all_pass, "checks": checks, "diagnostics": diagnostics}, ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
