from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.a2a.e2e.resource_selector import run_live_agui_resource_selector as runner
from scripts.a2a.e2e.resource_selector.run_live_agui_resource_selector import (
    SCENARIOS,
    _interrupts,
    _run_payload,
    _selector_interrupt,
)

# The global offline-test fixture replaces HOME and clears IAC_CODE_CONFIG_DIR.
# An explicitly opted-in live subprocess must retain the user's actual credential chain.
_REAL_HOME = os.environ.get("HOME")
_REAL_USERPROFILE = os.environ.get("USERPROFILE")
_REAL_CONFIG_DIR = os.environ.get("IAC_CODE_CONFIG_DIR")


def test_agui_live_matrix_covers_both_surfaces_and_all_answers() -> None:
    assert set(SCENARIOS) == {
        "normal-selected",
        "normal-canceled",
        "normal-direct-input",
        "pipeline-selected",
        "pipeline-canceled",
        "pipeline-direct-input",
    }


def test_agui_stream_trace_preserves_failure_order_without_payloads() -> None:
    events = [
        {"type": "RUN_STARTED", "runId": "private-run"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "private-cloud-body"},
        {"type": "CUSTOM", "name": "iac-code.pipeline.v1", "value": {
            "eventType": "step_failed", "data": {"error": "private-error"}}},
        {"type": "CUSTOM", "name": "iac-code.pipeline.v1", "value": {"eventType": "private-event"}},
        {"type": "RUN_ERROR", "code": "A2A_EXECUTION_FAILED", "message": "private-response"},
    ]
    assert runner._agui_stream_trace(events) == [
        "RUN_STARTED", "pipeline:step_failed", "RUN_ERROR:A2A_EXECUTION_FAILED",
    ]
    assert runner._agui_stream_trace([{"type": "RUN_ERROR", "code": "private-code"}]) == ["RUN_ERROR:other"]


def test_agui_request_keeps_safe_trace_and_still_rejects_run_error(monkeypatch) -> None:
    events = [{"type": "RUN_STARTED"}, {"type": "STEP_FINISHED"},
              {"type": "RUN_ERROR", "code": "A2A_EXECUTION_FAILED", "message": "private"}]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter(b"data: " + json.dumps(event).encode("utf-8") + b"\n" for event in events)

    monkeypatch.setattr(runner, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(AssertionError, match="AG-UI run error: A2A_EXECUTION_FAILED") as caught:
        runner._agui_request("http://fixture", {}, timeout=1)
    assert caught.value.agui_stream_trace == ["RUN_STARTED", "STEP_FINISHED", "RUN_ERROR:A2A_EXECUTION_FAILED"]


def test_live_payload_uses_actual_selling_pipeline_and_same_resume_coordinates(tmp_path: Path) -> None:
    arguments = {"thread_id": "thread-1", "invocation_id": "invocation-1", "cwd": tmp_path}
    first = _run_payload(**arguments, run_mode="pipeline", prompt="Choose a VPC")
    resumed = _run_payload(
        **arguments,
        run_mode="pipeline",
        resume=[{"interruptId": "resource-1", "status": "cancelled", "payload": {"optionsEmpty": False}}],
    )
    assert first["forwardedProps"]["iacCode"]["pipelineName"] == "selling_solution_first"
    assert resumed["forwardedProps"]["iacCode"]["pipelineName"] == "selling_solution_first"
    assert resumed["threadId"] == first["threadId"]
    assert resumed["forwardedProps"]["iacCode"]["rosInvocationId"] == "invocation-1"
    assert resumed["messages"] == []


def test_agui_live_selector_interrupt_reads_public_metadata() -> None:
    selector = {
        "id": "resource-1",
        "metadata": {"kind": "cloud_resource_selection", "selector": {"id": "vpc.vpc"}},
    }
    events = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [selector]}}]
    assert _interrupts(events) == [selector]
    assert _selector_interrupt(_interrupts(events)) == selector


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("failure", [None, "wrong-selector", "different-task", "missing-tool-result"])
def test_ci_summary_retains_native_agui_acceptance_and_failure_stage(tmp_path, monkeypatch, scenario, failure) -> None:
    monkeypatch.setattr(runner, "configuration_readiness", lambda **_: {"llm": {"ready": True},
                                                                     "cloud": {"ready": True}})

    class Process:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(runner, "ManagedServer", Process)
    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    monkeypatch.setattr(runner, "_free_port", lambda _: 12345)
    monkeypatch.setattr(runner, "wait_for_server", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_wait_agui", lambda *args: None)

    async def query(_metadata):
        return "vpc-fixture", "Fixture", 1, ["vpc-fixture"]

    monkeypatch.setattr(runner, "_query_real_vpc", query)
    monkeypatch.setattr(runner, "_advance_pipeline", lambda *args, initial, **kwargs: (initial, 2))
    metadata = {
        "kind": "cloud_resource_selection", "requestTaskId": "task", "contextId": "context",
        "toolUseId": "selector-call", "selector": {"id": "vpc.vpc"},
    }
    if failure == "wrong-selector":
        metadata["selector"]["id"] = "ecs.instance"
    interrupt = {"id": "input", "metadata": metadata, "responseSchema": {"type": "object"}}
    coordinates = {"type": "CUSTOM", "name": "iac-code.session.v1",
                   "value": {"taskId": "task", "contextId": "context"}}
    initial = [coordinates, {"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [interrupt]}}]
    resumed = [coordinates, {"type": "TOOL_CALL_RESULT", "toolCallId": "selector-call"},
               {"type": "RUN_FINISHED", "outcome": {"type": "success"}}]
    if failure == "different-task":
        resumed[0] = {**coordinates, "value": {"taskId": "other", "contextId": "context"}}
    if failure == "missing-tool-result":
        resumed.pop(1)
    calls = []

    def request(_url, payload, **kwargs):
        calls.append(payload)
        return initial if len(calls) == 1 else resumed

    monkeypatch.setattr(runner, "_agui_request", request)
    args = Namespace(run_dir=tmp_path / "scenario", model="glm-5.3-prime", scenario=scenario,
                     region="cn-hangzhou", turn_timeout=1)
    if failure:
        with pytest.raises(AssertionError):
            runner._run(args)
    else:
        runner._run(args)
        answer = calls[-1]["resume"][0]
        if scenario.endswith("canceled"):
            assert answer["status"] == "cancelled"
        elif scenario.endswith("direct-input"):
            assert answer["payload"] == {"freeText": "vpc-fixture"}
        else:
            assert answer["payload"] == {"value": "vpc-fixture", "label": "Fixture"}
    summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is (failure is None)
    if failure:
        expected = {"wrong-selector": "AGUI selector contract", "different-task": "AGUI same task resumed",
                    "missing-tool-result": "AGUI tool completed"}[failure]
        assert summary["checks"][expected] is False
        assert summary["error_type"] == "AssertionError"
        if failure == "different-task":
            assert summary["agui_failure_reason"] == "task_coordinates_changed"
    else:
        assert len(summary["checks"]) == 7
        assert all(summary["checks"].values())
    if scenario.startswith("pipeline-"):
        prompt = calls[0]["messages"][0]["content"]
        assert runner.PIPELINE_LOCAL_PERMISSION_INSTRUCTIONS in prompt
        assert "不创建任何云资源" in prompt and "云资源选择器" in prompt
        assert ("返回已有方案列表" in prompt) is (scenario == "pipeline-canceled")
    else:
        assert runner.PIPELINE_LOCAL_PERMISSION_INSTRUCTIONS not in calls[0]["messages"][0]["content"]


def test_failure_reason_retains_only_fixed_categories() -> None:
    assert runner._failure_reason(AssertionError("AG-UI did not publish the A2A session coordinates")) == (
        "session_coordinates_missing")
    assert runner._failure_reason(AssertionError("AG-UI run error: INTERNAL_ERROR")) == "agui_run_error:internal_error"
    assert runner._failure_reason(RuntimeError("private-provider-output")) == "other"


@pytest.mark.parametrize('state,expected', [('TASK_STATE_INPUT_REQUIRED', 'input-required'),
                                           ('TASK_STATE_FAILED', 'failed')])
def test_failed_resume_diagnostics_compare_native_pending_input_to_same_execution(
    tmp_path, monkeypatch, state, expected,
):
    calls = []

    class Client:
        def __init__(self, *, timeout_seconds):
            assert timeout_seconds == 5

        async def get_task(self, url, task_id, *, history_length):
            calls.append((url, task_id, history_length))
            return {'result': {'id': 'private-task', 'status': {'state': state}, 'metadata': {'iac_code': {
                'input': {'required': True, 'inputId': 'private-input', 'question': 'private-body'},
            }}}}

        async def aclose(self):
            calls.append('closed')

    monkeypatch.setattr(runner, 'A2AClient', Client)
    directory = tmp_path / 'agui-state'
    directory.mkdir()
    (directory / 'thread.json').write_text(json.dumps({
        'execution': {'taskId': 'private-task', 'executionId': 'current'},
        'appliedResumeDigests': [
            {'executionId': 'current', 'interruptId': 'private-input'},
            {'executionId': 'old', 'interruptId': 'other-input'},
        ],
    }), encoding='utf-8')
    result = asyncio.run(runner._failed_resume_diagnostics('http://fixture', 'private-task', tmp_path))
    assert result == {'aguiA2aTaskState': expected, 'aguiA2aPendingInputCount': 1,
                      'aguiA2aPendingAlreadyAppliedCount': 1}
    assert calls == [('http://fixture', 'private-task', 0), 'closed']
    assert 'private' not in json.dumps(result)


def test_failed_resume_public_summary_retains_only_state_and_counts():
    from scripts.ci.run_e2e import _public_live_summary

    summary = {'scenario': 'pipeline-canceled', 'passed': False,
               'checks': {'AGUI same task resumed': False},
               'aguiA2aTaskState': 'input-required', 'aguiA2aPendingInputCount': 1,
               'aguiA2aPendingAlreadyAppliedCount': 1, 'taskId': 'private-task', 'question': 'private-body',
               'leadInPendingReadOnlyPermissions': 1, 'leadInPendingShellPermissions': 0,
               'leadInPendingWorkspaceWrites': 0}
    public = _public_live_summary(summary, 'not-needed')
    assert public['status'] == 'failed' and public['checks']['AGUI same task resumed'] is False
    assert public['aguiA2aTaskState'] == 'input-required'
    assert public['aguiA2aPendingAlreadyAppliedCount'] == 1
    assert public['leadInPendingReadOnlyPermissions'] == 1 and public['leadInPendingShellPermissions'] == 0
    assert public['leadInPendingWorkspaceWrites'] == 0
    assert 'private' not in json.dumps(public)
    assert 'aguiA2aTaskState' not in _public_live_summary({**summary, 'aguiA2aTaskState': ['invalid']}, 'not-needed')
    assert 'leadInPendingShellPermissions' not in _public_live_summary(
        {**summary, 'leadInPendingShellPermissions': 'private-command'}, 'not-needed')


def test_pipeline_lead_in_resolves_permission_batch_without_authorizing_writes(tmp_path, monkeypatch) -> None:
    permissions = [{"id": key, "metadata": {"kind": "permission", "isReadOnly": readonly}}
                   for key, readonly in (("query", True), ("shell", False), ("unknown", None))]
    initial = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": permissions}}]
    selector = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [
        {"id": "selector", "metadata": {"kind": "cloud_resource_selection"}},
    ]}}]

    def request(_url, payload, **kwargs):
        assert {item["interruptId"]: item["payload"]["decision"] for item in payload["resume"]} == {
            "query": "allow_once", "shell": "deny", "unknown": "deny",
        }
        assert all(item["status"] == "resolved" for item in payload["resume"])
        return selector

    monkeypatch.setattr(runner, "_agui_request", request)
    events, turns = runner._advance_pipeline("http://fixture", initial=initial, thread_id="thread",
                                            invocation_id="invocation", cwd=tmp_path, timeout=1)
    assert events == selector
    assert turns == 1


def test_pipeline_diagnostics_distinguish_local_file_denial_without_exporting_paths(tmp_path, monkeypatch):
    pending = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [
        {"id": "private-input", "metadata": {"kind": "permission", "toolName": "write_file",
                                             "isReadOnly": False, "target": str(tmp_path.parent / 'private-path')}},
    ]}}]
    selector = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [
        {"id": "selector", "metadata": {"kind": "cloud_resource_selection"}},
    ]}}]
    def request(_url, payload, **kwargs):
        assert payload['resume'][0]['payload']['decision'] == 'deny'
        return selector
    monkeypatch.setattr(runner, '_agui_request', request)
    progress = {}
    runner._advance_pipeline('http://fixture', initial=pending, thread_id='thread', invocation_id='invocation',
                             cwd=tmp_path, timeout=1, progress=progress)
    assert progress['leadInDeniedLocalFilePermissions'] == 1
    assert progress['leadInDeniedShellPermissions'] == 0
    assert 'private' not in json.dumps(progress)


@pytest.mark.parametrize('tool', ['write_file', 'edit_file'])
def test_pipeline_allows_workspace_materialization_without_allowing_cloud_writes(tmp_path, monkeypatch, tool):
    pending = [{'type': 'RUN_FINISHED', 'outcome': {'type': 'interrupt', 'interrupts': [
        {'id': 'template', 'metadata': {'kind': 'permission', 'toolName': tool,
                                      'isReadOnly': False, 'target': str(tmp_path / 'template.yaml')}},
        {'id': 'cloud', 'metadata': {'kind': 'permission', 'toolName': 'aliyun_api', 'isReadOnly': False}},
    ]}}]
    selector = [{'type': 'RUN_FINISHED', 'outcome': {'type': 'interrupt', 'interrupts': [
        {'id': 'selector', 'metadata': {'kind': 'cloud_resource_selection'}},
    ]}}]
    def request(_url, payload, **kwargs):
        assert {v['interruptId']: v['payload']['decision'] for v in payload['resume']} == {
            'template': 'allow_once', 'cloud': 'deny'}
        return selector
    monkeypatch.setattr(runner, '_agui_request', request)
    progress = {}
    assert runner._advance_pipeline('http://fixture', initial=pending, thread_id='thread',
        invocation_id='invocation', cwd=tmp_path, timeout=1, progress=progress)[0] == selector
    assert progress['leadInAllowedWorkspaceFilePermissions'] == 1
    assert progress['leadInDeniedLocalFilePermissions'] == 0


@pytest.mark.parametrize('target', ['../outside.yaml', '', 'missing...yaml', '<redacted>',
                                   'one.yaml · two.yaml'])
def test_workspace_permission_requires_an_unambiguous_owned_target(tmp_path, target):
    assert runner._workspace_file_permission({'toolName': 'write_file', 'target': target}, tmp_path) is False


@pytest.mark.parametrize("question_turns", [0, 5])
def test_pipeline_inspects_last_permission_response_without_spending_question_budget(
    tmp_path, monkeypatch, question_turns,
) -> None:
    def interrupt(kind, identity='fixture'):
        return [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [
            {"id": identity, "metadata": {"kind": kind, "isReadOnly": True}},
        ]}}]

    sequence = [interrupt("permission", f'permission-{index}') for index in range(5)]
    sequence += [interrupt("ask_user_question") for _ in range(question_turns)]
    selector = interrupt("cloud_resource_selection")
    sequence.append(selector)
    initial = sequence.pop(0)
    monkeypatch.setattr(runner, "_agui_request", lambda *args, **kwargs: sequence.pop(0))
    progress = {}
    events, turns = runner._advance_pipeline(
        "http://fixture", initial=initial, thread_id="thread", invocation_id="invocation", cwd=tmp_path,
        timeout=1, progress=progress,
    )
    assert events == selector
    assert turns == 5 + question_turns
    assert progress["leadInPermissionTurns"] == 5
    assert progress.get("leadInConversationTurns", 0) == question_turns


@pytest.mark.parametrize("kind", ["permission", "ask_user_question"])
def test_pipeline_lead_in_still_fails_when_input_budget_is_exhausted(tmp_path, monkeypatch, kind) -> None:
    pending = [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": [
        {"id": "fixture", "metadata": {"kind": kind, "isReadOnly": True}},
    ]}}]
    monkeypatch.setattr(runner, "_agui_request", lambda *args, **kwargs: pending)
    progress = {}
    with pytest.raises(AssertionError, match="repeated resolved permission" if kind == 'permission' else "five"):
        runner._advance_pipeline("http://fixture", initial=pending, thread_id="thread", invocation_id="invocation",
                                cwd=tmp_path, timeout=1, progress=progress)
    if kind == "permission":
        assert progress["leadInPendingReadOnlyPermissions"] == 1
        assert progress["leadInPendingShellPermissions"] == 0
        assert progress["leadInPendingWorkspaceWrites"] == 0


def test_pipeline_allows_distinct_read_permissions_within_original_deadline(tmp_path, monkeypatch):
    def pending(identity, kind='permission'):
        return [{'type': 'RUN_FINISHED', 'outcome': {'type': 'interrupt', 'interrupts': [
            {'id': identity, 'metadata': {'kind': kind, 'isReadOnly': True}},
        ]}}]
    sequence = [pending(f'permission-{index}') for index in range(7)]
    selector = pending('selector', 'cloud_resource_selection')
    sequence.append(selector)
    initial = sequence.pop(0)
    calls = []
    def request(_url, payload, *, timeout):
        calls.append(payload)
        assert 0 < timeout <= 1
        return sequence.pop(0)
    monkeypatch.setattr(runner, '_agui_request', request)
    progress = {}
    events, _ = runner._advance_pipeline('http://fixture', initial=initial, thread_id='thread',
        invocation_id='invocation', cwd=tmp_path, timeout=1, progress=progress)
    assert events == selector and len(calls) == 7
    assert progress['leadInPermissionTurns'] == 7
    assert all(call['resume'][0]['payload'] == {'decision': 'allow_once'} for call in calls)


def test_pipeline_distinct_permissions_cannot_extend_original_deadline(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runner.time, 'monotonic', lambda: clock[0])
    def pending(identity):
        return [{'type': 'RUN_FINISHED', 'outcome': {'type': 'interrupt', 'interrupts': [
            {'id': identity, 'metadata': {'kind': 'permission', 'isReadOnly': True}},
        ]}}]
    calls = []
    def request(_url, _payload, *, timeout):
        calls.append(timeout)
        clock[0] += 0.6
        return pending(f'permission-{len(calls)}')
    monkeypatch.setattr(runner, '_agui_request', request)
    with pytest.raises(AssertionError, match='pre-selector deadline'):
        runner._advance_pipeline('http://fixture', initial=pending('initial'), thread_id='thread',
            invocation_id='invocation', cwd=tmp_path, timeout=1)
    assert calls == pytest.approx([1.0, 0.4])


@pytest.mark.integration
@pytest.mark.resource_selector_live
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_real_agui_http_sse_resource_selector_matrix(tmp_path: Path, scenario: str) -> None:
    if os.environ.get("IAC_CODE_AGUI_RESOURCE_SELECTOR_LIVE_E2E", "").lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("explicit real AG-UI resource-selector E2E is disabled")
    env = os.environ.copy()
    if _REAL_HOME is not None:
        env["HOME"] = _REAL_HOME
    if _REAL_USERPROFILE is not None:
        env["USERPROFILE"] = _REAL_USERPROFILE
    if _REAL_CONFIG_DIR is None:
        env.pop("IAC_CODE_CONFIG_DIR", None)
    else:
        env["IAC_CODE_CONFIG_DIR"] = _REAL_CONFIG_DIR
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts/a2a/e2e/resource_selector/run_live_agui_resource_selector.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--allow-real-cloud",
            "--scenario",
            scenario,
            "--run-dir",
            str(tmp_path / scenario),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["passed"] is True
    assert result["scenario"] == scenario
    assert result["selectorId"] == "vpc.vpc"
    assert result["candidateCount"] > 0
    assert result["interruptIssued"] is True
    assert result["toolCompleted"] is True
    assert result["continuationObserved"] is True
    assert result["usedRealLlm"] is True
    assert result["usedRealCloudQuery"] is True
    assert result["usedAguiHttpSse"] is True
