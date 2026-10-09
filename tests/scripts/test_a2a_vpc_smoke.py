import json
import subprocess

import pytest

from scripts.a2a.smoke import test_a2a_vpc as smoke


def test_sync_diagnostics_detect_status_text_masking_final_agent_output_without_exporting_text(monkeypatch):
    calls = []
    task = {
        "id": "private-task",
        "status": {"state": "TASK_STATE_COMPLETED", "message": {"parts": [{"text": "done-private"}]}},
        "history": [
            {"role": "ROLE_USER", "parts": [{"text": "private request"}]},
            {"role": "ROLE_AGENT", "parts": [{"text": '{"Resources":{"Type":"ALIYUN::ECS::VPC"}}'}]},
        ],
        "metadata": {"token": "private-secret"},
    }

    def read(command, *options):
        calls.append((command, options))
        return {"tasks": [{"id": "private-task"}]} if command == "task-list" else {"task": task}

    monkeypatch.setattr(smoke, "_read_task_diagnostic", read)
    result = smoke._sync_failure_diagnostics("done-private")
    assert result["smoke_sync_task_state"] == "completed"
    assert result["smoke_sync_history_vpc_marker"] is True
    assert result["smoke_sync_status_text_matches_stdout"] is True
    assert result["smoke_sync_task_probe_category"] == "verified"
    assert [command for command, _ in calls] == ["task-list", "task-get"]
    assert "private" not in json.dumps(result)


def test_sync_diagnostics_do_not_use_old_agent_output_across_a_user_message(monkeypatch):
    task = {
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "history": [
            {"role": "ROLE_AGENT", "parts": [{"text": "old VPC template"}]},
            {"role": "ROLE_USER", "parts": [{"text": "new request"}]},
        ],
    }
    monkeypatch.setattr(smoke, "_read_task_diagnostic", lambda command, *args:
                        {"tasks": [{"id": "private-task"}]} if command == "task-list" else task)
    result = smoke._sync_failure_diagnostics("需要权限")
    assert result["smoke_sync_permission_hint"] is True
    assert result["smoke_sync_task_state"] == "input-required"
    assert result["smoke_sync_history_vpc_marker"] is False
    assert result["smoke_sync_trailing_agent_message_count"] == 0


def test_sync_diagnostics_do_not_guess_between_tasks(monkeypatch):
    calls = []

    def read(command, *options):
        calls.append(command)
        return {"tasks": [{"id": "one"}, {"id": "two"}]}

    monkeypatch.setattr(smoke, "_read_task_diagnostic", read)
    result = smoke._sync_failure_diagnostics("done")
    assert calls == ["task-list"]
    assert result["smoke_sync_task_probe_category"] == "task_ambiguous"


def test_sync_diagnostic_timeout_keeps_the_original_failed_acceptance(monkeypatch):
    monkeypatch.setattr(smoke, "run_a2a_client_call", lambda prompt:
                        subprocess.CompletedProcess([], 0, "done", ""))

    def unavailable(*args):
        raise subprocess.TimeoutExpired("read-only probe", 5)

    monkeypatch.setattr(smoke, "_read_task_diagnostic", unavailable)
    checks, diagnostics = {}, {}
    smoke.test_call_sync(checks, diagnostics)
    assert checks["sync call succeeded"] is True
    assert checks["output contains VPC-related content"] is False
    assert diagnostics["smoke_sync_task_probe_category"] == "query_failed"


def test_successful_sync_call_does_not_add_a_probe(monkeypatch):
    monkeypatch.setattr(smoke, "run_a2a_client_call", lambda prompt:
                        subprocess.CompletedProcess([], 0, "VPC template", ""))
    monkeypatch.setattr(smoke, "_read_task_diagnostic", lambda *args: pytest.fail("unexpected query"))
    checks, diagnostics = {}, {}
    smoke.test_call_sync(checks, diagnostics)
    assert all(checks.values())
    assert diagnostics == {}


def test_read_task_diagnostic_uses_bounded_read_only_cli_without_logging_payload(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"result":{"tasks":[]}}', "")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    assert smoke._read_task_diagnostic("task-list", "--output", "json") == {"tasks": []}
    command, options = calls[0]
    assert command[4] == "task-list"
    assert options["timeout"] == 5 and options["encoding"] == "utf-8"


@pytest.mark.parametrize('location', ['part', 'nested_part', 'normal_task_metadata'])
def test_pending_input_diagnostic_uses_structured_envelope_and_excludes_target(monkeypatch, location):
    envelope = {'kind': 'permission', 'toolName': 'write_file', 'isReadOnly': False,
                'target': '/private/template.json', 'inputId': 'private-id', 'prompt': 'private-secret'}
    data = {'input': envelope} if location == 'nested_part' else envelope
    task = {'status': {'state': 'TASK_STATE_INPUT_REQUIRED', 'message': {'parts': [{'data': data}]}}}
    if location == 'normal_task_metadata':
        task = {'status': {'state': 'TASK_STATE_INPUT_REQUIRED'}, 'metadata': {'iac_code': {'input': envelope}}}
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'private-task'}]} if command == 'task-list' else task)
    result = smoke._sync_failure_diagnostics('done')
    assert result['smoke_sync_pending_kind'] == 'permission'
    assert result['smoke_sync_pending_tool'] == 'write_file'
    assert result['smoke_sync_pending_read_only'] is False
    assert 'private' not in json.dumps(result)


def _permission_task(workspace, **updates):
    pending = {'kind': 'permission', 'schemaVersion': 1, 'toolName': 'write_file',
               'effect': 'file_change', 'isReadOnly': False, 'target': str(workspace / 'template.json'),
               'requestTaskId': 'task-one', 'contextId': 'context-one',
               'inputId': 'input-one', 'toolUseId': 'tool-one', **updates}
    return {'id': 'task-one', 'contextId': 'context-one', 'status': {'state': 'TASK_STATE_INPUT_REQUIRED'},
            'metadata': {'iac_code': {'input': pending}}}


def test_sync_smoke_resolves_native_workspace_permission_before_checking_template(monkeypatch, tmp_path):
    calls = []
    clock = iter([10, 11, 12])
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: next(clock))

    def cli(prompt, **options):
        calls.append((prompt, options))
        output = 'Input required.' if len(calls) == 1 else '{"Type":"ALIYUN::ECS::VPC"}'
        return subprocess.CompletedProcess([], 0, output, '')

    monkeypatch.setattr(smoke, '_run_cli_call', cli)
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else _permission_task(tmp_path))
    checks, diagnostics = {}, {}
    smoke.test_call_sync(checks, diagnostics)
    assert all(checks.values()) and len(calls) == 2
    assert diagnostics == {'smoke_sync_permission_response_count': 1}
    payload = json.loads(calls[1][0].removeprefix('IAC_CODE_PERMISSION:'))
    assert payload == {'schemaVersion': 1, 'kind': 'permission', 'requestTaskId': 'task-one',
                       'contextId': 'context-one', 'inputId': 'input-one', 'toolUseId': 'tool-one',
                       'decision': 'allow_once'}
    assert calls[1][1]['context_id'] == 'context-one'
    assert calls[1][1]['timeout'] == smoke.TIMEOUT_SECONDS - 2


@pytest.mark.parametrize('updates', [
    {'toolName': 'ros_deploy', 'effect': 'cloud_change'},
    {'toolName': 'bash', 'isReadOnly': True},
    {'target': '../outside.json'},
    {'kind': 'ask_user_question'},
    {'requestTaskId': 'other-task'},
    {'contextId': 'other-context'},
    {'inputId': ''},
])
def test_sync_smoke_does_not_approve_unrelated_or_unowned_operations(monkeypatch, tmp_path, updates):
    calls = []
    monkeypatch.setattr(smoke, '_run_cli_call', lambda prompt, **options:
                        calls.append(prompt) or subprocess.CompletedProcess([], 0, 'Input required.', ''))
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list'
                        else _permission_task(tmp_path, **updates))
    checks = {}
    smoke.test_call_sync(checks)
    assert len(calls) == 1
    assert checks['output contains VPC-related content'] is False


def test_sync_smoke_rejects_replayed_native_input_without_waiting_for_timeout(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(smoke, '_run_cli_call', lambda prompt, **options:
                        calls.append(prompt) or subprocess.CompletedProcess([], 0, 'Input required.', ''))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else _permission_task(tmp_path))
    result = smoke.run_a2a_client_call('template only', cwd=str(tmp_path))
    assert result.stdout == 'Input required.' and len(calls) == 2


@pytest.mark.parametrize('target_shape', ['absolute', 'relative', 'multiple', 'opaque', 'missing'])
def test_permission_failure_diagnostics_show_exact_closed_gates_without_target(monkeypatch, tmp_path, target_shape):
    target = {'absolute': str(tmp_path / 'private-template.json'), 'relative': 'private-template.json',
              'multiple': 'private-template.json · private-template.json',
              'opaque': '<private-template.json>', 'missing': None}[target_shape]
    task = _permission_task(tmp_path, target=target)
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else task)
    result = smoke._sync_failure_diagnostics('done')
    assert result['smoke_sync_permission_metadata_present'] is True
    assert result['smoke_sync_permission_schema_supported'] is True
    assert result['smoke_sync_permission_task_matches'] is True
    assert result['smoke_sync_permission_context_matches'] is True
    assert result['smoke_sync_permission_identity_complete'] is True
    assert result['smoke_sync_permission_workspace_allowed'] is (target_shape in {'absolute', 'relative'})
    assert result['smoke_sync_permission_target_shape'] == target_shape
    assert result['smoke_sync_permission_effect'] == 'file_change'
    assert 'private' not in json.dumps(result)


def test_permission_failure_diagnostics_keep_identity_failures_and_opaque_effect_closed(monkeypatch, tmp_path):
    task = _permission_task(tmp_path, schemaVersion=True, requestTaskId='different-task',
                            contextId='different-context', inputId='', effect={'private': 'secret'})
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else task)
    result = smoke._sync_failure_diagnostics('done')
    for key in ('schema_supported', 'task_matches', 'context_matches', 'identity_complete', 'workspace_allowed'):
        assert result['smoke_sync_permission_' + key] is False
    assert result['smoke_sync_permission_effect'] == 'other'
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('version, supported', [
    (1, True), (1.0, True), (True, False), (False, False), ('1', False), ('1.0', False),
    (None, False), (0, False), (2, False), (1.1, False), (float('nan'), False), (float('inf'), False),
])
def test_native_permission_schema_supports_struct_numbers_and_rejects_other_versions(version, supported):
    assert smoke._permission_schema_supported({'schemaVersion': version}) is supported


def test_sync_permission_continues_real_a2a_protobuf_task_without_weakening_identity_or_scope(monkeypatch, tmp_path):
    from a2a.types import Task, TaskState
    from google.protobuf.json_format import MessageToDict, ParseDict

    from iac_code.a2a.input_required import permission_input_envelope
    from iac_code.types.stream_events import PermissionRequestEvent

    envelope = permission_input_envelope(
        PermissionRequestEvent(tool_name='write_file', tool_input={'path': str(tmp_path / 'template.json'),
                                                                  'content': '{}'}, tool_use_id='tool-one'),
        task_id='task-one', context_id='context-one', input_id='input-one', language='en',
    )
    task = Task(id='task-one', context_id='context-one')
    task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
    ParseDict({'iac_code': {'input': envelope}}, task.metadata)
    wire = MessageToDict(Task.FromString(task.SerializeToString()))
    pending = wire['metadata']['iac_code']['input']
    assert type(pending['schemaVersion']) is float and pending['schemaVersion'] == 1.0
    calls = []

    def cli(prompt, **options):
        calls.append((prompt, options))
        return subprocess.CompletedProcess([], 0, 'Input required.' if len(calls) == 1 else 'VPC template', '')

    monkeypatch.setattr(smoke, '_run_cli_call', cli)
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else {'task': wire})
    with monkeypatch.context() as old_logic:
        old_logic.setattr(smoke, '_permission_schema_supported', lambda pending:
                          type(pending.get('schemaVersion')) is int and pending['schemaVersion'] == 1)
        stopped = smoke.run_a2a_client_call('VPC JSON template', cwd=str(tmp_path))
        assert stopped.stdout == 'Input required.' and len(calls) == 1
    calls.clear()
    result = smoke.run_a2a_client_call('VPC JSON template', cwd=str(tmp_path))
    assert result.stdout == 'VPC template' and result.smoke_permission_response_count == 1
    reply = json.loads(calls[1][0].removeprefix('IAC_CODE_PERMISSION:'))
    assert reply == {key: pending[key] for key in ('schemaVersion', 'kind', 'requestTaskId',
                                                'contextId', 'inputId', 'toolUseId')} | {'decision': 'allow_once'}
    assert calls[1][1]['context_id'] == 'context-one'

    # An authentic Struct number does not authorize an unrelated workspace.
    calls.clear()
    result = smoke.run_a2a_client_call('VPC JSON template', cwd=str(tmp_path / 'other'))
    assert result.stdout == 'Input required.' and len(calls) == 1
