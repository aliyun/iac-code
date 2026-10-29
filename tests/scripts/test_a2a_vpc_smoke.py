import io
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


def _completed_task(text='{"Type":"ALIYUN::ECS::VPC"}'):
    return {'id': 'task-one', 'contextId': 'context-one', 'status': {'state': 'TASK_STATE_COMPLETED'},
            'history': [{'role': 'ROLE_AGENT', 'parts': [{'text': text}]}]}


def _task_flow(monkeypatch, tasks):
    calls, probes = [], []
    states = iter(tasks)
    clock = [0.0]
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(smoke.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def cli(prompt, **options):
        calls.append((prompt, options))
        # Both the initial prompt and the receipt contain the old early-exit markers.
        return subprocess.CompletedProcess([], 0, 'VPC template permission receipt', '')

    def read(command, *options, **kwargs):
        probes.append((command, options, kwargs))
        return {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else next(states)

    monkeypatch.setattr(smoke, '_run_cli_call', cli)
    monkeypatch.setattr(smoke, '_read_task_diagnostic', read)
    monkeypatch.setattr(smoke, '_normal_turn_completed', lambda *args, **kwargs: False)
    return calls, probes, clock


@pytest.mark.parametrize('transition', ['old-input', 'working', 'multiple', 'acknowledged'])
def test_sync_smoke_polls_permission_progress_and_only_validates_final_task(monkeypatch, tmp_path, transition):
    first = _permission_task(tmp_path)
    intermediate = _permission_task(tmp_path)
    expected_answers = 1
    if transition == 'working':
        intermediate = _completed_task()
        intermediate['status'] = {'state': 'TASK_STATE_WORKING'}
        intermediate.pop('history')
    elif transition == 'multiple':
        intermediate = _permission_task(tmp_path, inputId='input-two', toolUseId='tool-two')
        expected_answers = 2
    elif transition == 'acknowledged':
        intermediate['metadata']['iac_code']['permissionAck'] = {
            'accepted': True, 'inputId': 'input-one', 'toolUseId': 'tool-one', 'decision': 'allow_once',
        }
    calls, probes, _clock = _task_flow(monkeypatch, [first, intermediate, _completed_task('final VPC JSON')])
    result = smoke.run_a2a_client_call('JSON template only', cwd=str(tmp_path))
    assert result.returncode == 0 and result.stdout == 'final VPC JSON'
    assert len(calls) == 1 + expected_answers
    assert result.smoke_permission_response_count == expected_answers
    assert len(probes) == 4
    assert result.smoke_sync_diagnostics['smoke_sync_task_state'] == 'completed'
    if transition == 'acknowledged':
        assert result.smoke_sync_diagnostics['smoke_sync_permission_confirmation'] == 'acknowledged'
    answers = [json.loads(prompt.removeprefix('IAC_CODE_PERMISSION:')) for prompt, _ in calls[1:]]
    assert len({answer['inputId'] for answer in answers}) == expected_answers


@pytest.mark.parametrize('state', ['FAILED', 'CANCELED', 'REJECTED', 'AUTH_REQUIRED'])
def test_sync_smoke_terminal_failure_never_validates_permission_receipt(monkeypatch, tmp_path, state):
    ended = _completed_task('VPC from a failed task must not pass')
    ended['status'] = {'state': 'TASK_STATE_' + state}
    calls, _probes, _clock = _task_flow(monkeypatch, [_permission_task(tmp_path), ended])
    result = smoke.run_a2a_client_call('JSON template only', cwd=str(tmp_path))
    assert result.returncode == 1 and result.stdout == '' and len(calls) == 2
    assert result.smoke_sync_diagnostics['smoke_sync_exit_reason'] == 'task_ended'


def test_sync_smoke_does_not_use_permission_receipt_when_final_output_has_no_template(monkeypatch, tmp_path):
    _task_flow(monkeypatch, [_permission_task(tmp_path), _completed_task('Done')])
    monkeypatch.setattr(smoke, '_sync_failure_diagnostics', lambda *_: {})
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    checks = {}
    smoke.test_call_sync(checks)
    assert checks['sync call succeeded'] is True
    assert checks['output contains VPC-related content'] is False


def test_completed_task_does_not_read_old_template_across_a_user_message():
    task = _completed_task('old VPC template')
    task['history'].append({'role': 'ROLE_USER', 'parts': [{'text': 'permission response'}]})
    assert smoke._completed_task_text(task) == ''


def test_normal_turn_completion_is_not_mistaken_for_an_unanswered_input(monkeypatch, tmp_path):
    task = _completed_task('final VPC JSON')
    task['status']['state'] = 'TASK_STATE_INPUT_REQUIRED'
    task['metadata'] = {'iac_code': {'assistantFinal': {'complete': True}}}
    calls, _probes, _clock = _task_flow(monkeypatch, [_permission_task(tmp_path), task])
    result = smoke.run_a2a_client_call('JSON only', cwd=str(tmp_path))
    assert result.returncode == 0 and result.stdout == 'final VPC JSON' and len(calls) == 2
    assert result.smoke_sync_diagnostics['smoke_sync_exit_reason'] == 'completed'


def test_normal_turn_without_final_metadata_requires_natural_completion_proof(monkeypatch, tmp_path):
    final = '{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}'
    task = _completed_task(final)
    task['status']['state'] = 'TASK_STATE_INPUT_REQUIRED'
    task['history'].insert(0, {'role': 'ROLE_AGENT', 'parts': [{'text': 'partial VPC delta'}]})
    _task_flow(monkeypatch, [_permission_task(tmp_path), task, task])
    observed = iter([False, True])
    monkeypatch.setattr(smoke, '_normal_turn_completed', lambda *args, **kwargs: next(observed))
    result = smoke.run_a2a_client_call('JSON only', cwd=str(tmp_path))
    assert result.returncode == 0 and result.stdout == final
    assert result.smoke_permission_response_count == 1


@pytest.mark.parametrize('cli_text, passed', [
    ('{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}', True),
    ('```json\n{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}\n```', True),
    ('Template:\n```json\n{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}\n```', False),
    ('VPC permission accepted', False), ('{"kind":"permission_ack","target":"vpc.json"}', False),
])
def test_finished_owner_with_stale_permission_requires_template_not_receipt(monkeypatch, tmp_path, cli_text, passed):
    pending = _permission_task(tmp_path)
    calls, _probes, _clock = _task_flow(monkeypatch, [pending, pending])
    monkeypatch.setattr(smoke, '_normal_turn_completed', lambda *args, **kwargs: True)

    def cli(prompt, **options):
        calls.append((prompt, options))
        return subprocess.CompletedProcess([], 0, cli_text if len(calls) == 2 else 'Input required.', '')

    monkeypatch.setattr(smoke, '_run_cli_call', cli)
    result = smoke.run_a2a_client_call('JSON only', cwd=str(tmp_path))
    assert (result.returncode == 0) is passed and len(calls) == 2
    assert result.stdout == (cli_text if passed else '')


@pytest.mark.parametrize('text, passed', [
    ('Template:\n```json\n{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}\n```\nDone.', True),
    ('Template:\n```\n{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}\n```', True),
    ('Template: {"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}', False),
    ('Template:\n```json\n{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}\n```', False),
    ('Template:\n```json\n{"Resources":{"Subnet":{"Type":"ALIYUN::ECS::VSwitch"}}}\n```', False),
    ('Receipt:\n```json\n{"kind":"permission_ack","target":"vpc.json"}\n```', False),
    ('Envelope:\n```json\n{"result":{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}}\n```', False),
])
def test_resolved_permission_accepts_complete_fenced_final_template(monkeypatch, tmp_path, text, passed):
    task = _completed_task(text)
    task['status']['state'] = 'TASK_STATE_INPUT_REQUIRED'
    task['metadata'] = {'iac_code': {'input': None, 'pendingPermissions': []}}
    calls, _probes, _clock = _task_flow(monkeypatch, [_permission_task(tmp_path), task])
    monkeypatch.setattr(smoke, '_normal_turn_completed', lambda *args, **kwargs: True)
    result = smoke.run_a2a_client_call('JSON only', cwd=str(tmp_path))
    assert (result.returncode == 0) is passed and len(calls) == 2
    assert result.stdout == (text if passed else '')


def test_fenced_template_is_not_extracted_while_other_permissions_remain():
    text = 'Template:\n```json\n{"Resources":{"VPC":{"Type":"ALIYUN::ECS::VPC"}}}\n```'
    task = _completed_task(text)
    task['metadata'] = {'iac_code': {'input': None, 'pendingPermissions': [{'toolUseId': 'other-tool'}]}}
    assert smoke._normal_turn_output(task, text, permission_submitted=True) == ''


@pytest.mark.parametrize('updates, completed', [
    ({}, True), ({'taskId': 'other-task'}, False), ({'contextId': 'other-context'}, False),
    ({'phase': 'running'}, False), ({'terminationReason': 'explicit'}, False), ({'releaseReady': False}, False),
])
def test_normal_turn_completion_probe_is_read_only_and_requires_matching_released_owner(
    monkeypatch, updates, completed,
):
    control = dict(contextId='context-one', taskId='task-one', phase='terminated',
                   terminationReason='natural_completion', releaseReady=True)
    control.update(updates)
    calls = []

    def read(url, **options):
        calls.append((url, options))
        return io.BytesIO(json.dumps(control).encode('utf-8'))

    monkeypatch.setattr(smoke.urllib.request, 'urlopen', read)
    assert smoke._normal_turn_completed(_completed_task(), timeout=2) is completed
    assert calls == [(smoke.A2A_URL + '/iac-code/execution/state?contextId=context-one', {'timeout': 2})]


def test_client_subprocess_does_not_extend_remaining_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(smoke.subprocess, 'run', lambda command, **options:
                        calls.append(options) or subprocess.CompletedProcess(command, 0, '', ''))
    smoke._run_cli_call('JSON only', timeout=2)
    assert calls[0]['timeout'] == 2


def test_normal_owner_proof_keeps_original_sync_output_when_no_permission_was_submitted():
    task = _completed_task('last partial delta')
    final = 'Final ROS VPC template reply'
    assert smoke._normal_turn_output(task, final, permission_submitted=False) == final


def test_sync_smoke_probe_and_authorization_share_original_timeout(monkeypatch, tmp_path):
    calls, probes, clock = _task_flow(monkeypatch, [
        _permission_task(tmp_path), _permission_task(tmp_path),
        dict(_completed_task(), status={'state': 'TASK_STATE_WORKING'}), _completed_task(),
    ])
    monkeypatch.setattr(smoke, 'TIMEOUT_SECONDS', 2)
    result = smoke.run_a2a_client_call('JSON template only', cwd=str(tmp_path))
    assert clock[0] == 2 and result.returncode == 1
    assert result.smoke_sync_diagnostics['smoke_sync_exit_reason'] == 'timeout'
    assert len(calls) == 2
    assert all(0 < options['timeout'] <= 2 for _command, _args, options in probes)


def test_sync_smoke_diagnostics_never_export_task_payload(monkeypatch, tmp_path, capsys):
    task = _completed_task('private response')
    task['status'] = {'state': 'unrecognized-private-state'}
    task['metadata'] = {'token': 'private-credential'}
    _task_flow(monkeypatch, [task])
    result = smoke.run_a2a_client_call('private prompt', cwd=str(tmp_path))
    assert result.smoke_sync_diagnostics['smoke_sync_task_state'] == 'unknown'
    assert 'private' not in json.dumps(result.smoke_sync_diagnostics)
    assert 'private' not in capsys.readouterr().out


def test_template_prompt_requires_final_json_and_confines_optional_files(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    prompt = smoke._template_prompt()
    assert '最终直接回复完整JSON模板正文' in prompt
    assert '禁止写到目录外' in prompt and str(tmp_path.resolve()) in prompt
    assert '不创建或部署云资源' in prompt


def test_sync_smoke_resolves_native_workspace_permission_before_checking_template(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: 10)

    def cli(prompt, **options):
        calls.append((prompt, options))
        output = 'Input required.' if len(calls) == 1 else '{"Type":"ALIYUN::ECS::VPC"}'
        return subprocess.CompletedProcess([], 0, output, '')

    monkeypatch.setattr(smoke, '_run_cli_call', cli)
    monkeypatch.setattr(smoke, 'A2A_WORKSPACE', str(tmp_path))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args, **kwargs:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else
                        (_permission_task(tmp_path) if len(calls) == 1 else _completed_task()))
    checks, diagnostics = {}, {}
    smoke.test_call_sync(checks, diagnostics)
    assert all(checks.values()) and len(calls) == 2
    assert diagnostics['smoke_sync_permission_response_count'] == 1
    assert diagnostics['smoke_sync_exit_reason'] == 'completed'
    assert diagnostics['smoke_sync_permission_confirmation'] == 'task_advanced'
    payload = json.loads(calls[1][0].removeprefix('IAC_CODE_PERMISSION:'))
    assert payload == {'schemaVersion': 1, 'kind': 'permission', 'requestTaskId': 'task-one',
                       'contextId': 'context-one', 'inputId': 'input-one', 'toolUseId': 'tool-one',
                       'decision': 'allow_once'}
    assert calls[1][1]['context_id'] == 'context-one'
    assert calls[1][1]['timeout'] == smoke.TIMEOUT_SECONDS


@pytest.mark.parametrize('updates', [
    {'toolName': 'ros_deploy', 'effect': 'cloud_change'},
    {'toolName': 'aliyun_api', 'effect': 'cloud_change'},
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
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args, **kwargs:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list'
                        else _permission_task(tmp_path, **updates))
    checks = {}
    smoke.test_call_sync(checks)
    assert len(calls) == 1
    assert checks['output contains VPC-related content'] is False


def test_sync_smoke_waits_for_replayed_native_input_without_reauthorizing_until_deadline(monkeypatch, tmp_path):
    calls = []
    clock = [0.0]
    monkeypatch.setattr(smoke, 'TIMEOUT_SECONDS', 3)
    monkeypatch.setattr(smoke.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(smoke.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(smoke, '_normal_turn_completed', lambda *args, **kwargs: False)
    monkeypatch.setattr(smoke, '_run_cli_call', lambda prompt, **options:
                        calls.append(prompt) or subprocess.CompletedProcess([], 0, 'Input required.', ''))
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args, **kwargs:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else _permission_task(tmp_path))
    result = smoke.run_a2a_client_call('template only', cwd=str(tmp_path))
    assert result.returncode == 1 and result.stdout == '' and len(calls) == 2
    assert clock[0] == 3
    assert result.smoke_sync_diagnostics['smoke_sync_exit_reason'] == 'timeout'
    assert result.smoke_sync_diagnostics['smoke_sync_same_permission_pending'] is True


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
    monkeypatch.setattr(smoke, '_read_task_diagnostic', lambda command, *args, **kwargs:
                        {'tasks': [{'id': 'task-one'}]} if command == 'task-list' else
                        {'task': wire if len(calls) == 1 else _completed_task('VPC template')})
    with monkeypatch.context() as old_logic:
        old_logic.setattr(smoke, '_permission_schema_supported', lambda pending:
                          type(pending.get('schemaVersion')) is int and pending['schemaVersion'] == 1)
        stopped = smoke.run_a2a_client_call('VPC JSON template', cwd=str(tmp_path))
        assert stopped.returncode == 1 and len(calls) == 1
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
    assert result.returncode == 1 and len(calls) == 1
