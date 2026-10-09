from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from scripts.ci.live_diagnostics import collect_live_diagnostics


@pytest.mark.parametrize(("timed_out", "return_code", "text", "categories"), [
    (True, None, "private partial model response", []),
    (False, 1, "RateLimitError: private token and account", ["rate_limit"]),
    (False, 1, "BadRequestError: private request and model", ["bad_request"]),
    (False, 1, "AuthenticationError: private key", ["authentication"]),
    (False, 1, "APIConnectionError: private host", ["connection"]),
    (False, 1, "ReadTimeout: private url", ["provider_timeout"]),
    (False, 1, "InternalServerError: private body", ["provider_server"]),
    (False, 0, "OK and private metadata", []),
])
def test_preflight_facts_keep_timeout_and_closed_error_categories_only(
    tmp_path, timed_out, return_code, text, categories,
):
    (tmp_path / "preflight.json").write_text(json.dumps({
        "ok": return_code == 0, "timedOut": timed_out, "returnCode": return_code,
        "elapsedSeconds": 60.125, "stdout": text, "stderr": "private stderr", "summary": "private summary",
    }), encoding="utf-8")
    diagnostic = collect_live_diagnostics(tmp_path, {})["llm_preflight"]
    assert diagnostic == {
        "ok": return_code == 0, "timedOut": timed_out, "elapsedSeconds": 60.125,
        "errorCategories": categories, **({"returnCode": return_code} if return_code is not None else {}),
    }
    assert "private" not in json.dumps(diagnostic)


def test_invalid_preflight_json_cannot_hide_other_diagnostics(tmp_path):
    (tmp_path / "preflight.json").write_text("not json", encoding="utf-8")
    assert "llm_preflight" not in collect_live_diagnostics(tmp_path, {})


def test_cidr_trace_keeps_call_order_and_results_without_duplicating_mirrored_transcripts(tmp_path):
    old, new = '10.64.0.0/24', '10.64.0.128/25'
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'edit_file', 'id': 'private-edit',
            'input': {'path': '/private/file', 'old_string': old, 'new_string': new, 'token': 'private-secret'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-edit',
                                   'content': 'private-result', 'is_error': False}]},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'complete_step', 'id': 'private-complete',
            'input': {'conclusion': {'status': 'awaiting_confirmation', 'hard_constraint_checks': [
                {'actual_value': new, 'status': 'conflict'}]}}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-complete',
                                   'content': 'private-result', 'is_error': True}]},
    ]
    for name in ('original', 'mirror'):
        path = tmp_path / 'pipeline/transcripts' / name / 'session.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    old_hash = hashlib.sha256(old.encode()).hexdigest()
    new_hash = hashlib.sha256(new.encode()).hexdigest()
    assert facts['cidr_tool_input_trace'] == [
        {'tool': 'edit_file', 'step': 'unknown', 'old_string_cidr_hashes': [old_hash],
         'new_string_cidr_hashes': [new_hash], 'has_result': True, 'is_error': False},
        {'tool': 'complete_step', 'step': 'unknown', 'conclusion_cidr_hashes': [new_hash],
         'status': 'awaiting_confirmation', 'has_result': True, 'is_error': True},
    ]
    text = json.dumps(facts)
    assert 'private' not in text and old not in text and new not in text


def test_cidr_trace_orders_persisted_attempts_before_unindexed_mirrors(tmp_path):
    def write(name, call_id, status):
        path = tmp_path / 'pipeline/transcripts' / name / 'session.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'role': 'assistant', 'content': [{'type': 'tool_use',
            'name': 'complete_step', 'id': call_id, 'input': {'conclusion': {'status': status}}}]}), encoding='utf-8')

    write('transcript_att_0003', 'private-final', 'confirmed')
    write('transcript_att_0001', 'private-first', 'awaiting_confirmation')
    write('mirrored-final', 'private-final', 'confirmed')
    facts = collect_live_diagnostics(tmp_path, {})
    assert [row['status'] for row in facts['cidr_tool_input_trace']] == ['awaiting_confirmation', 'confirmed']


@pytest.mark.parametrize('runtime_config', [False, True])
def test_native_a2a_journal_diagnostics_keep_real_boundaries_without_payloads(tmp_path, runtime_config):
    root = tmp_path / 'run'
    root.mkdir()
    config = tmp_path / 'config' if runtime_config else None
    base = config if config is not None else root
    journal = base / 'sessions/private-session/a2a/pipeline/a2a-events.jsonl'
    journal.parent.mkdir(parents=True)
    waiting = {'eventType': 'input_required', 'status': 'input_required', 'visibility': 'committed',
               'taskId': 'private-task', 'data': {'body': 'private-secret'},
               'input': {'kind': 'candidate_selection', 'inputId': 'private-input', 'prompt': 'private-body'}}
    failed = {'eventType': 'pipeline_failed', 'status': 'failed', 'visibility': 'pending_backup',
              'data': {'errorSummary': 'private-error'}}
    journal.write_text('\n'.join(json.dumps(row) for row in [
        waiting, {'__iac_code_record_type': 'event_group', 'events': [failed]},
        {'eventType': ['invalid']}, {'eventType': 'input_required', 'input': {'kind': ['invalid']}},
        {'eventType': 'private-event', 'status': 'private-status'},
    ]) + '\npartial', encoding='utf-8')
    facts = collect_live_diagnostics(root, {}, runtime_config_dir=config)
    assert facts['native_a2a_journal_boundaries'] == [
        {'type': 'input_required', 'status': 'input_required', 'visibility': 'committed',
         'inputKind': 'candidate_selection'},
        {'type': 'pipeline_failed', 'status': 'failed', 'visibility': 'pending_backup'},
        {'type': 'input_required'},
    ]
    assert 'private' not in json.dumps(facts)
    assert journal.read_text(encoding='utf-8').endswith('partial')


@pytest.mark.parametrize('error_type, summary, expected', [
    ('TypeError', "TypeError: unhashable type: 'dict' private-secret", {'errorCategories': ['unhashable_type']}),
    ('KeyError', "KeyError: 'candidate_index' private-secret", {
        'errorCategories': ['missing_mapping_key'], 'errorFields': ['candidate_index']}),
    ('ValueError', 'ValueError: private-secret', {}),
    ('private-secret', 'private-secret', {}),
])
def test_native_a2a_failure_diagnostics_keep_only_closed_error_facts(tmp_path, error_type, summary, expected):
    journal = tmp_path / 'a2a/pipeline/a2a-events.jsonl'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'eventType': 'pipeline_failed', 'status': 'failed', 'data': {
        'source': 'executor', 'errorSummary': summary,
        'errorDetails': {'type': error_type, 'errorId': 'private-secret', 'traceback': 'private-secret'},
    }}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    item = facts['native_a2a_journal_boundaries'][0]
    assert item == {'type': 'pipeline_failed', 'status': 'failed', 'source': 'executor', **expected,
                    **({'errorType': error_type} if error_type != 'private-secret' else {})}
    assert 'private' not in json.dumps(facts)


def test_constraint_failure_checkpoint_keeps_relations_without_private_values(tmp_path):
    pipeline = tmp_path / 'pipeline'
    pipeline.mkdir()
    check = {
        'constraint': {'id': 'private-id', 'target': 'VSwitch', 'property': 'CidrBlock', 'operator': 'eq',
                       'value': '10.64.0.0/24', 'verification_mode': 'direct', 'source_text': 'private-secret'},
        'status': 'conflict', 'actual_value': '10.64.0.128/25', 'evidence': [{'secret': 'private'}],
    }
    other = {'constraint': {'id': 'private-other', 'property': 'private-password', 'value': 'private-value'},
             'status': 'unresolved', 'actual_value': 'private-password', 'parameter_values': {'secret': 'private'}}
    (pipeline / 'context.yaml').write_text(yaml.safe_dump({'selected_plan': {'value': {
        'selected_candidate_result': {'cost': {'hard_constraint_checks': [check, other]}}}}}), encoding='utf-8')
    transcript = pipeline / 'transcripts/step/session.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'complete_step', 'id': 'private-call',
                                        'input': {'conclusion': {'status': 'confirmed'}}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-call', 'is_error': True,
            'content': 'Every explicit user hard constraint must be covered. '
                       'constraint_comparison_failed[private-id]; constraint_not_satisfied[private-id]'}]},
    ]), encoding='utf-8')

    facts = collect_live_diagnostics(tmp_path, {})
    first, second = facts['completion_checkpoint_constraint_checks']
    assert first['source'] == 'current_checkpoint'
    assert first['target'] == 'VSwitch' and first['property_kind'] == 'cidrblock'
    assert first['llm_status'] == 'conflict' and first['code_comparison_passed'] is False
    assert first['actual_is_subnet_of_expected'] is True and first['expected_is_subnet_of_actual'] is False
    assert first['actual_cidr_hash'] == hashlib.sha256(b'10.64.0.128/25').hexdigest()
    assert second['property_kind'] == 'other' and not any(key.endswith('_hash') for key in second)
    text = json.dumps(facts)
    assert 'private' not in text and '10.64.' not in text


def test_native_constraint_error_diagnostic_exports_only_fixed_issue_codes(tmp_path):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step',
            'input': {'conclusion': {'status': 'confirmed'}}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
            'content': 'Every explicit user hard constraint must be covered. Validation issue: '
                       'multiple_constraint_issues (constraint_copy_mismatch[private-id]; '
                       'constraint_parameter_mismatch[private-id, private-value]; private_custom_issue[secret]).'}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_constraint_issue_counts'] == {
        'constraint_copy_mismatch': 1, 'constraint_parameter_mismatch': 1,
    }
    assert 'private' not in json.dumps(facts) and 'secret' not in json.dumps(facts)


def test_failed_stack_vpc_diagnostic_keeps_fixed_kinds_and_valid_hashes(tmp_path):
    digest = hashlib.sha256(b'vpc-private-reference').hexdigest()
    (tmp_path / 'before.ros-stack-states.json').write_text(json.dumps({'private-stack': {
        'status': 'CREATE_FAILED', 'status_reason': 'Forbidden.VpcNotFound private-text',
        'vpc_reference_diagnostic': {
            'template_resources_inspected': True,
            'vswitch_vpc_reference_kinds': {'literal': 1, 'private-kind': 1},
            'vswitch_vpc_reference_hashes': [digest, 'private-id'], 'private': 'private-template',
            'vswitch_zone_reference_kinds': {'parameter': 1, 'private': 1},
            'vswitch_zone_region_categories': {'stack_region': 1, 'private': 1},
            'vpc_presence_after_failure': {'available': 1, 'private': 1},
        },
    }}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['ros_stack_template_inspection_counts'] == {'inspected': 1}
    assert facts['ros_stack_vswitch_vpc_reference_kinds'] == {'literal': 1}
    assert facts['ros_stack_vswitch_vpc_reference_hashes'] == [digest]
    assert facts['ros_stack_vswitch_zone_reference_kinds'] == {'parameter': 1}
    assert facts['ros_stack_vswitch_zone_region_categories'] == {'stack_region': 1}
    assert facts['ros_stack_vpc_presence_after_failure'] == {'available': 1}
    assert 'private' not in json.dumps(facts)


def test_stack_region_and_fixture_lifecycle_diagnostics_do_not_export_private_fields(tmp_path):
    (tmp_path / '.e2e-network-fixture-diagnostic.json').write_text(json.dumps({
        'network_fixture_available_before_run': True, 'CreationTime': 'private-time',
        'VpcId': 'private-vpc', 'private': 'private-secret'}), encoding='utf-8')
    (tmp_path / 'before.ros-stack-states.json').write_text(json.dumps({
        'private-stack': {'status': 'CREATE_FAILED', 'region_id': 'cn-hangzhou',
                          'status_reason': 'Forbidden.VpcNotFound private-secret'},
        'private-other': {'status': 'CREATE_COMPLETE', 'region_id': 'private-region'},
    }), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['network_fixture_available_before_run'] is True
    assert facts['ros_stack_region_categories'] == {'fixture_region': 1, 'other_region': 1}
    assert facts['ros_stack_failure_known_codes'] == {'Forbidden.VpcNotFound': 1}
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize('filename', ['server-1.stderr.log', 'a2a.stderr.log', 'agui.stderr.log'])
def test_invalid_path_exception_diagnostic_keeps_category_without_private_text(tmp_path, filename):
    (tmp_path / filename).write_text(
        'Traceback (most recent call last):\n'
        '  File "/private/install/iac_code/utils/public_paths.py", line 334, in _candidate_norm_paths\n'
        '    real_path = posixpath.realpath(absolute)\n'
        'ValueError: lstat: embedded null character in path private-secret\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['server_exception_types'] == {'ValueError': 1}
    assert facts['server_exception_categories'] == {'invalid_path_null_byte': 1}
    assert 'private' not in json.dumps(facts)


def test_externalized_deployment_error_projects_actual_cause_without_body(tmp_path):
    external = tmp_path / 'tool-results/private-result.json'
    external.parent.mkdir()
    external.write_text('CREATE_FAILED InvalidVSwitchCidr private-credential private-vpc', encoding='utf-8')
    transcript = tmp_path / 'pipeline/transcripts/deploy/session.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call', 'name': 'ros_deploy'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'is_error': True,
            'content': 'CREATE_FAILED Full result saved to private-result.json',
            'metadata': {'_iac_code_externalized_result_path': str(external)}}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['cloud_tool_error_by_tool'] == {'ros_deploy:create_failed': 1, 'ros_deploy:invalid_cidr': 1}
    assert facts['cloud_tool_error_result_shapes'] == {'external_result_read': 1}
    assert 'private' not in json.dumps(facts)


def test_externalized_deployment_error_does_not_read_outside_evidence_roots(tmp_path):
    root = tmp_path / 'case'
    transcript = root / 'pipeline/transcripts/deploy/session.jsonl'
    transcript.parent.mkdir(parents=True)
    external = tmp_path / 'private-result.json'
    external.write_text('InvalidVSwitchCidr private-credential', encoding='utf-8')
    transcript.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call', 'name': 'ros_deploy'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'is_error': True,
            'content': 'CREATE_FAILED', 'metadata': {'_iac_code_externalized_result_path': str(external)}}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(root, {})
    assert facts['cloud_tool_error_by_tool'] == {'ros_deploy:create_failed': 1}
    assert facts['cloud_tool_error_result_shapes'] == {'external_result_unavailable': 1}
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize(('reason', 'category'), [
    ('judge failed: timeout after 90s private-key', 'judge_timeout'),
    ('parse failed: invalid action private-response', 'judge_parse_failure'),
    ('judge failed while executing side-effect step private; pipeline paused for safety. '
     'judge failed: ConnectionError private-endpoint', 'judge_failure'),
    ('用户消息与当前任务无关 private-data', 'unrelated_input'),
])
def test_interrupt_diagnostic_distinguishes_judge_failure_from_continue(tmp_path, reason, category):
    (tmp_path / 'interrupt.events.jsonl').write_text(json.dumps({
        'eventType': 'interrupt_classified', 'sequence': 10,
        'data': {'action': 'continue', 'reason': reason, 'paused': True},
    }), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['rollback_event_trace'][0]['reasonCategory'] == category
    assert facts['rollback_event_trace'][0]['paused'] is True
    assert 'private' not in json.dumps(facts)


def test_rejected_native_completion_projects_decision_without_reason_or_images(tmp_path):
    from iac_code.a2a.pipeline_events import _event_data

    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'role': 'assistant', 'content': [{'type': 'tool_use',
        'name': 'complete_step', 'id': 'private-call', 'input': {'conclusion': {
            'status': 'rejected', 'continue_pipeline': False, 'is_infra_intent': False,
            'rejection_reason': '用户说本轮不部署 private-request', 'secret': 'private-key'}}}]}), encoding='utf-8')
    (tmp_path / 'initial.events.jsonl').write_text(json.dumps({'eventType': 'pipeline_completed',
        'data': _event_data({'early_exit': True, 'failed': False, 'private': 'private-response'})}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_decision_inputs'] == [{'step': 'unknown', 'status': 'rejected',
        'continue_pipeline': False, 'is_infra_intent': False, 'rejection_categories': ['no_deployment']}]
    assert facts['native_a2a_terminal_events'] == [{'type': 'pipeline_completed', 'failed': False, 'early_exit': True}]
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize(('reason', 'category'), [
    ('检测到提示词注入 private-data', 'instruction_injection'),
    ('信息不足 private-data', 'missing_information'),
    ('安全限制 private-data', 'policy_restriction'),
    ('不支持当前请求 private-data', 'unsupported_request'),
])
def test_rejection_diagnostics_export_only_fixed_categories(tmp_path, reason, category):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'role': 'assistant', 'content': [{'type': 'tool_use',
        'name': 'complete_step', 'id': 'private-call', 'input': {'conclusion': {
            'status': 'rejected', 'rejection_reason': reason}}}]}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_decision_inputs'][0]['rejection_categories'] == [category]
    assert 'private' not in json.dumps(facts)


def test_malformed_model_decision_does_not_abort_diagnostic_collection(tmp_path):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'complete_step',
        'id': 'private-call', 'input': {'conclusion': {'status': {'private': 'response'}}}}]}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert 'completion_decision_inputs' not in facts
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize(('content', 'shape'), [
    ('{"Resources": [], "private": "private-password"}', {'json_object': 1, 'Resources': 1}),
    ('{"error": "private-password", "success": false}',
     {'json_object': 1, 'error': 1, 'success': 1, 'failure_boolean': 1}),
    ('Full output saved to /private/tool-results/private-id.json',
     {'non_json_text': 1, 'external_result_reference': 1}),
])
def test_quote_diagnostic_keeps_native_shape_not_prices_paths_or_secrets(tmp_path, content, shape):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'price', 'name': 'ros_estimate_template_cost'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'price',
                                    'is_error': False, 'content': content}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['quote_native_result_shapes'] == shape
    assert 'private' not in json.dumps(facts)


def test_native_stack_failure_reason_is_projected_without_cloud_id_name_or_body(tmp_path):
    (tmp_path / 'acceptance.ros-stack-states.json').write_text(json.dumps({'private-stack-id': {
        'stack_id': 'private-stack-id', 'stack_name': 'private-name', 'status': 'CREATE_FAILED',
        'status_reason': 'InvalidVSwitchCidr private-network private-credential'}}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['ros_stack_observed_status_counts'] == {'CREATE_FAILED': 1}
    assert facts['ros_stack_failure_categories'] == {'invalid_cidr': 1}
    assert 'private' not in json.dumps(facts)


def test_quote_guard_shape_reads_local_external_result_without_exporting_body(tmp_path):
    external = tmp_path / 'tool-results/private-result.json'
    external.parent.mkdir()
    external.write_text(json.dumps({'Resources': [], 'OriginalAmount': '12.34',
                                    'private-key': 'private-secret'}), encoding='utf-8')
    transcript = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    transcript.parent.mkdir(parents=True)
    rows = [{'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'ros_estimate_template_cost', 'input': {}},
    ]}, {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': False,
         'content': 'Full result saved to private-result.json',
         'metadata': {'_iac_code_externalized_result_path': str(external)}},
    ]}]
    transcript.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['quote_guard_result_shapes'] == {'external_result_read': 1, 'resources_array': 1, 'amount_present': 1}
    assert 'private' not in json.dumps(facts) and '12.34' not in json.dumps(facts)


def test_legacy_schema_type_failure_keeps_public_field_without_rejected_value(tmp_path):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step'},
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
         'content': "private-secret is not of type 'array'\nOn instance['selected_review_aspects']:"},
    ]}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_schema_fields'] == ['selected_review_aspects']
    assert facts['completion_schema_validators'] == ['type']
    assert 'private' not in json.dumps(facts)


def test_stack_failure_code_projection_keeps_unknown_codes_and_ids_private(tmp_path):
    (tmp_path / 'acceptance.ros-stack-states.json').write_text(json.dumps({'private-id': {
        'status': 'CREATE_FAILED',
        'status_reason': 'Forbidden.CidrBlock VSwitch CidrBlock private-value Forbidden.private-secret',
    }}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['ros_stack_failure_known_codes'] == {'Forbidden.CidrBlock': 1}
    assert facts['ros_stack_failure_fields'] == {'CidrBlock': 1, 'VSwitch': 1}
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize('form', ['single', 'structured'])
def test_schema_failure_binds_actual_input_type_without_exporting_values(tmp_path, form):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    inputs = {'conclusion': {'intent': {'hard_constraints': [{'unit': None, 'value': 'private-value'}]}}}
    message = ("None is not of type 'string'\nOn instance['intent']['hard_constraints'][0]['unit']:"
               if form == 'single' else json.dumps({'validator': 'type',
               'path': '/intent/hard_constraints/0/unit', 'message': 'private-message'}))
    path.write_text(json.dumps({'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step', 'input': inputs},
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True, 'content': message},
    ]}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_schema_value_types'] == [{'path': 'intent/hard_constraints/[]/unit',
        'source': 'conclusion', 'actual_type': 'null', 'step': 'unknown'}]
    assert 'private' not in json.dumps(facts)


def test_schema_type_trace_omits_unknown_property_coordinates(tmp_path):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step',
         'input': {'conclusion': {'intent': {'private-secret': 'private-value'}}}},
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
         'content': '{"path":"/intent/private-secret", "validator":"type"}'},
    ]}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert 'completion_schema_value_types' not in facts
    assert 'private' not in json.dumps(facts)


def test_fixture_vpc_can_be_compared_to_native_deploy_input_without_exporting_identity(tmp_path):
    vpc_hash = hashlib.sha256(b'private-vpc').hexdigest()
    (tmp_path / '.e2e-network-fixture-diagnostic.json').write_text(json.dumps({
        'network_fixture_vpc_hash': vpc_hash, 'private': 'secret'}), encoding='utf-8')
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'ros_deploy',
        'id': 'private-call', 'input': {'action': 'create', 'parameters': {'VpcId': 'private-vpc'}}}]}),
        encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['network_fixture_vpc_hash'] == vpc_hash
    assert facts['deployment_input_identity_trace'][0]['vpc_id_hash'] == vpc_hash
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize(('reason', 'category', 'code'), [
    ('Forbidden.VpcNotFound VpcId private-vpc', 'resource_missing', 'Forbidden.VpcNotFound'),
    ('InvalidVpcId.NotFound private-vpc', 'resource_missing', 'InvalidVpcId.NotFound'),
    ('IncorrectVSwitchStatus private-vswitch', 'operation_conflict', 'IncorrectVSwitchStatus'),
    ('Forbidden.RAM private-account', 'permission', 'Forbidden.RAM'),
    ('Forbidden.OperateShareResource private-vpc', 'shared_resource', 'Forbidden.OperateShareResource'),
    ('Forbidden private-account', 'permission', 'Forbidden'),
])
def test_native_stack_error_category_distinguishes_missing_vpc_from_permission(tmp_path, reason, category, code):
    (tmp_path / 'acceptance.ros-stack-states.json').write_text(json.dumps({'private-id': {
        'status': 'CREATE_FAILED', 'status_reason': reason,
    }}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['ros_stack_failure_categories'] == {category: 1}
    assert facts['ros_stack_failure_known_codes'] == {code: 1}
    assert 'private' not in json.dumps(facts)


def test_rpc_exception_diagnostic_is_a_fixed_projection_not_a_traceback(tmp_path):
    (tmp_path / 'initial.events.jsonl').write_text(json.dumps({'error': {
        'code': -32603, 'message': 'private-text',
        'data': 'Traceback (most recent call last) private-path/executor.py KeyError: private-secret'}}),
        encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['jsonrpc_exception_types'] == ['KeyError']
    assert facts['jsonrpc_exception_sites'] == ['executor.py']
    assert 'private' not in json.dumps(facts)


def test_native_preview_and_price_failures_export_fixed_categories_not_payloads(tmp_path):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    rows = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'ros_preview_template', 'id': 'preview', 'input': {'secret': 'private'}},
            {'type': 'tool_use', 'name': 'ros_estimate_template_cost', 'id': 'price', 'input': {'secret': 'private'}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'preview', 'is_error': True,
             'content': 'InvalidDBInstanceClass DBInstanceClass=private-sku VpcId=private-vpc private-password'},
            {'type': 'tool_result', 'tool_use_id': 'price', 'is_error': True,
             'content': 'ProductNotFound price for private-product MasterUserPassword=private-password'}]},
        {'role': 'user', 'content': [
            {'type': 'tool_use', 'name': 'ros_preview_template', 'id': 'spoof'},
            {'type': 'tool_result', 'tool_use_id': 'spoof', 'is_error': True, 'content': 'InvalidTemplate'}]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['cloud_tool_error_by_tool'] == {
        'ros_preview_template:invalid_database_spec': 1,
        'ros_estimate_template_cost:price_unavailable': 1}
    assert facts['cloud_tool_error_parameter_fields'] == {
        'DBInstanceClass': 1, 'VpcId': 1, 'MasterUserPassword': 1}
    assert 'private' not in json.dumps(facts)


def test_deployment_identity_diagnostics_hash_only_actual_tool_inputs(tmp_path):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    (path.parents[2] / 'meta.yaml').write_text(yaml.safe_dump({'attempts': {'items': {
        'private-attempt': {'scope': 'parent', 'step_id': 'deploying', 'transcript_id': 'transcript_att_0001'}}}}),
        encoding='utf-8')
    rows = [
        {'role': 'system', 'content': [{'type': 'text', 'text': '# E2E fixture isolation\nprivate-config'}]},
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'complete_step', 'input': {'conclusion': {'intent': {
                'non_functional': {'stack_name': 'private-expected', 'api_key': 'private-secret'}}}}},
            {'type': 'tool_use', 'name': 'ros_deploy', 'input': {'action': 'create',
                'stack_name': 'private-actual', 'template_url': 'private-url',
                'parameters': {'key': 'private-secret'}}},
            {'type': 'tool_use', 'name': 'ros_deploy', 'input': {'action': 'continue_create',
                'stack_id': 'private-stack-id'}}]},
        {'role': 'user', 'content': [{'type': 'tool_use', 'name': 'ros_deploy',
                                    'input': {'action': 'private-action', 'stack_name': 'private-fake'}}]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()
    assert facts['deployment_input_identity_trace'] == [
        {'step': 'deploying', 'action': 'create', 'stack_name_hash': digest('private-actual'),
         'name_suffix_kind': 'alphanumeric'},
        {'step': 'deploying', 'action': 'continue_create', 'stack_id_hash': digest('private-stack-id')}]
    assert facts['completion_intent_stack_name_trace'] == [
        {'step': 'deploying', 'stack_name_hash': digest('private-expected')}]
    assert facts['fixture_instruction_transcript_count'] == 1
    assert 'private-' not in json.dumps(facts)


@pytest.mark.parametrize('form', ['text', 'blocks', 'external'])
@pytest.mark.parametrize(('reason', 'expected'), [
    ('ZoneId is mandatory; private-value', {'missing': 1}),
    ('InvalidZoneId.NotFound private-value', {'not_found': 1, 'invalid': 1}),
    ('OperationDenied.ZoneIsDisabled ZoneId private-value', {'disabled': 1}),
    ('ZoneId private-value is not supported', {'unsupported': 1}),
    ('InvalidParameter ZoneId private-value', {'invalid': 1}),
    ('ZoneId private-value rejected', {'unknown': 1}),
])
def test_failed_deployment_zone_diagnostic_projects_only_native_reason_categories(tmp_path, reason, expected, form):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    content = json.dumps({'status': 'CREATE_FAILED', 'status_reason': reason, 'stack_id': 'private-stack'})
    result = {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True, 'content': content}
    if form == 'blocks':
        result['content'] = [{'type': 'text', 'text': content}]
    elif form == 'external':
        external = tmp_path / 'private-result.json'
        external.write_text(content, encoding='utf-8')
        result.update(content='saved to private-path', metadata={'_iac_code_externalized_result_path': str(external)})
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'ros_deploy', 'id': 'private-id'}]},
        {'role': 'user', 'content': [result]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['deployment_zone_failure_categories'] == expected
    assert 'private' not in json.dumps(facts)


def test_zone_diagnostic_does_not_infer_reason_from_schema_or_success(tmp_path):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'ros_deploy', 'id': 'call'}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'call', 'is_error': True,
             'content': json.dumps({'schema': 'ZoneId is mandatory private-text'})},
            {'type': 'tool_result', 'tool_use_id': 'call', 'is_error': False,
             'content': json.dumps({'status_reason': 'ZoneId is mandatory private-text'})}]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert 'deployment_zone_failure_categories' not in facts
    assert 'private' not in json.dumps(facts)


def test_rpc_failure_exports_only_protocol_code_and_known_markers(tmp_path):
    (tmp_path / 'initial.events.jsonl').write_text(json.dumps({'error': {
        'code': -32001, 'message': 'active session context private-secret', 'data': 'private-payload'}}),
        encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['jsonrpc_error_code'] == -32001
    assert facts['jsonrpc_error_markers'] == ['active session', 'context']
    assert 'private-' not in json.dumps(facts)


def test_flat_legacy_intent_keeps_exact_name_constraint_without_exporting_values(tmp_path):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    (path.parents[2] / 'meta.yaml').write_text(yaml.safe_dump({'attempts': {'items': {'private-attempt': {
        'scope': 'parent', 'step_id': 'intent_parsing', 'transcript_id': 'transcript_att_0001'}}}}), encoding='utf-8')
    path.write_text(json.dumps({'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'complete_step',
        'input': {'conclusion': {'hard_constraints': [{'property': 'stack_name', 'operator': 'eq',
            'value': 'private-required-name', 'source': 'user', 'source_text': 'private-prompt'}]}}}]}),
        encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_intent_stack_name_trace'] == [{'step': 'intent_parsing', 'source': 'exact_constraint',
        'stack_name_hash': hashlib.sha256(b'private-required-name').hexdigest()}]
    assert 'private-' not in json.dumps(facts)


def test_rollback_trace_preserves_wire_order_but_no_private_payloads(tmp_path):
    rows = [
        {"eventType": "step_started", "sequence": 8.0, "step": {"id": "intent_parsing"}},
        {"eventType": "interrupt_classified", "sequence": 10.0,
         "data": {"action": "hard_interrupt", "targetStepId": "architecture_planning", "reason": "private"}},
        {"eventType": "rollback_completed", "sequence": 11.0,
         "data": {"toStepId": "architecture_planning", "secret": "private"}},
        {"eventType": "step_started", "sequence": 12.0, "step": {"id": "architecture_planning"}},
        {"eventType": "step_started", "sequence": 14.0, "step": {"id": "private-step"}},
        {"eventType": "step_started", "sequence": True},
        {"eventType": "step_started", "sequence": 13.5},
    ]
    payloads = [{"metadata": {"iac_code": {"pipeline": row}}} for row in reversed(rows)]
    for filename in ("initial.events.jsonl", "rollback.events.jsonl"):
        (tmp_path / filename).write_text("\n".join(json.dumps(p) for p in payloads), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {"abort_reason":
        "Timed out waiting for post-image-rollback step_started(intent_parsing); last_error=private"})
    assert facts["failed_wait"] == "post-image-rollback step_started(intent_parsing)"
    assert facts["rollback_event_trace"] == [
        {"eventType": "step_started", "sequence": 8, "stepId": "intent_parsing"},
        {"eventType": "interrupt_classified", "sequence": 10,
         "rollbackTarget": "architecture_planning", "action": "hard_interrupt"},
        {"eventType": "rollback_completed", "sequence": 11, "rollbackTarget": "architecture_planning"},
        {"eventType": "step_started", "sequence": 12, "stepId": "architecture_planning"},
        {"eventType": "step_started", "sequence": 14},
    ]
    assert "private" not in json.dumps(facts)


@pytest.mark.parametrize("reason, code", [
    ("stream_error", "model_stream_error"), ("max_turns", "model_turn_limit"),
    ("length", "model_output_limit"), ("max_tokens", "model_output_limit"),
])
def test_model_termination_diagnostics_export_fixed_categories_only(tmp_path, reason, code):
    meta = tmp_path / "config/projects/p/s/pipeline/meta.yaml"
    meta.parent.mkdir(parents=True)
    meta.write_text(yaml.safe_dump({"reason":
        f"No conclusion extracted (agent stop reason: {reason}) private-token"}), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["pipeline_reason_codes"] == ["no_conclusion", code]
    assert "private-token" not in json.dumps(facts)


def test_stack_polling_diagnostics_keep_native_status_and_hashed_identity_only(tmp_path):
    rows = [{'metadata': {'iac_code': {'pipeline': {
        'eventType': 'stack_progress', 'data': {
            'status': status, 'stackId': 'private-stack', 'stackName': 'private-name',
            'resources': [{'private': 'secret'}], 'toolUseId': 'private-tool',
        }}}}} for status in ('UPDATE_COMPLETE', 'UPDATE_COMPLETE', 'private-status')]
    (tmp_path / 'recovered.events.jsonl').write_text(
        '\n'.join(json.dumps(row) for row in rows), encoding='utf-8'
    )
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['native_stack_progress_status_counts'] == {'UPDATE_COMPLETE': 2}
    assert list(facts['native_stack_progress_last_statuses'].values()) == ['UPDATE_COMPLETE']
    assert all(len(key) == 64 for key in facts['native_stack_progress_last_statuses'])
    assert 'private' not in json.dumps(facts) and 'secret' not in json.dumps(facts)


def test_candidate_ui_trace_distinguishes_key_submission_from_engine_resume(tmp_path):
    display = tmp_path / 'config/projects/p/s/pipeline/display.jsonl'
    display.parent.mkdir(parents=True)
    types = ('step_started', 'step_completed', 'candidate_selection_ready', 'candidate_selection_submitted')
    rows = [{'type': kind, 'step_id': 'confirm_and_select', 'payload': {'private': 'secret'}} for kind in types]
    rows.append({'type': 'candidate_selected', 'step_id': 'private-step'})
    display.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['candidate_ui_trace'] == [{'type': kind, 'step': 'confirm_and_select'} for kind in types]
    assert 'private' not in json.dumps(facts) and 'secret' not in json.dumps(facts)


def test_missing_conclusion_facts_keep_only_public_tools_and_bounded_nudges(tmp_path):
    transcript = tmp_path / "config/projects/p/s/pipeline/transcripts/transcript_att_0002/session.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [{"role": "assistant", "content": [
        {"type": "text", "text": "private reasoning"},
        {"type": "tool_use", "id": "private-id", "name": "read", "input": {"path": "private-path"}},
        {"type": "tool_use", "id": "private-other", "name": "private-tool", "input": {}}]},
        {"content": [{"type": "tool_result", "tool_use_id": "private-id", "is_error": True,
                      "content": "private cloud log"}]}]
    transcript.write_text("\n".join(json.dumps(v) for v in rows), encoding="utf-8")
    (transcript.parents[2] / "meta.yaml").write_text(yaml.safe_dump({
        "current_step": "architecture_planning", "attempts": {"items": {"private-attempt": {
            "scope": "parent", "step_id": "architecture_planning", "transcript_id": "transcript_att_0002"}}}}),
        encoding="utf-8")
    (tmp_path / "server-1.stderr.log").write_text(
        "Pipeline step nudge issued: step_id=architecture_planning nudge_count=2 max_nudges=2 session_id=private-id\n"
        "Pipeline step nudge issued: step_id=private-step nudge_count=2 max_nudges=2 session_id=private-id\n",
        encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["completion_tool_use_counts"] == {"read": 1, "other": 1}
    assert facts["completion_tool_error_counts"] == {"read": 1}
    assert facts["completion_assistant_text_turn_count"] == 1
    assert facts["completion_step_tool_use_counts"] == {"architecture_planning:read": 1,
                                                      "architecture_planning:other": 1}
    assert facts["completion_step_text_turn_counts"] == {"architecture_planning": 1}
    assert facts["pending_step"] == "architecture_planning"
    assert facts["completion_nudge_counts"] == {"architecture_planning": 2}
    assert "private" not in json.dumps(facts)


def test_local_file_permission_failure_has_known_tool_name_and_no_body(tmp_path):
    transcript = tmp_path / 'config/projects/p/s/pipeline/transcripts/transcript_att_0002/session.jsonl'
    transcript.parent.mkdir(parents=True)
    rows = [{'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'write_file', 'input': {'path': 'private-path'}},
    ]}, {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
         'content': 'Permission denied: private-path'},
    ]}]
    transcript.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_tool_use_counts'] == {'write_file': 1}
    assert facts['completion_tool_error_counts'] == {'write_file': 1}
    assert facts['permission_denied_tool_counts'] == {'write_file': 1}
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize(('body', 'category'), [
    ('Unknown tool: write_file private', 'unknown_tool'),
    ('未知工具：write_file private', 'unknown_tool'),
    ("Invalid input for tool 'write_file': private", 'invalid_input'),
    ('Permission denied: private', 'permission_denied'),
    ('File write failed: private', 'other'),
])
def test_tool_failure_category_uses_real_call_and_attempt_step_without_body(tmp_path, body, category):
    pipeline = tmp_path / 'pipeline'
    path = pipeline / 'transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    (pipeline / 'meta.yaml').write_text(yaml.safe_dump({'attempts': {'items': {'1': {
        'scope': 'parent', 'step_id': 'solution_planning_and_selection', 'transcript_id': 'transcript_att_0001',
    }}}}), encoding="utf-8")
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'write_file', 'id': 'private-call'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'unmatched-private',
                                     'is_error': True, 'content': 'Unknown tool: private'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-call',
                                     'is_error': True, 'content': body}]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_step_tool_error_categories'] == {
        'solution_planning_and_selection:write_file:' + category: 1,
    }
    assert 'private' not in json.dumps(facts)


def test_diagnostics_distinguish_incidental_candidate_text_and_real_events(tmp_path: Path) -> None:
    path = tmp_path / "turn.events.jsonl"
    path.write_text(json.dumps({"message": {"text": "candidate_step_started fake-secret"}}), encoding="utf-8")
    assert collect_live_diagnostics(tmp_path, {})["candidate_marker_without_event"] is True
    path.write_text(json.dumps({"pipeline": {"eventType": "candidate_step_started", "data": {"text": "fake-secret"}}}),
                    encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["candidate_marker_without_event"] is False
    assert facts["a2a_event_counts"] == {"candidate_step_started": 1}
    assert "fake-secret" not in json.dumps(facts)


def test_diagnostics_keep_cleanup_categories_and_wait_names_without_raw_bodies(tmp_path: Path) -> None:
    (tmp_path / "cleanup-result.json").write_text(json.dumps({
        "resources": [{"stackId": "private-stack"}], "deletedStackIds": [],
        "failures": ["private-stack: ownership could not be proven", "private-stack: cleanup subprocess exited 1",
                     "observed Stack ownership outside exact manifest or cleanup incomplete"],
    }), encoding="utf-8")
    (tmp_path / "cleanup-private-stack.log").write_text("NotFound.Stack private-key private-stack", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(json.dumps({
        "type": "expect", "passed": False, "description": "candidate selection controls ready", "tail": "private-key",
    }) + "\n" + json.dumps({"type": "expect", "passed": False, "description": "private-key"}), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {
        "abort_reason": "TimeoutError: candidate selection input was not accepted",
    })
    assert facts["cleanup_failure_categories"] == {"ownership_unproven": 2, "delete_subprocess_failed": 1}
    assert facts["cleanup_known_codes"] == ["NotFound.Stack"]
    assert facts["cleanup_resource_count"] == 1
    assert facts["cleanup_deleted_count"] == 0
    assert facts["failed_wait"] == "candidate selection controls ready"
    assert facts["abort_type"] == "TimeoutError"
    assert facts["abort_category"] == "selection_not_accepted"
    assert "private-" not in json.dumps(facts)


def test_diagnostics_report_durable_unanswered_input_and_image_confirmation(tmp_path: Path) -> None:
    meta = tmp_path / "config/projects/p/s/pipeline/meta.yaml"
    meta.parent.mkdir(parents=True)
    meta.write_text(yaml.safe_dump({"current_step": "materialize_selected_candidate", "execution": {
        "pending_input_kind": "ask_user_question", "pending_ask_user_question_input": {
            "question": "private-question", "toolUseId": "private-id",
        },
    }}), encoding="utf-8")
    (tmp_path / "turn.events.jsonl").write_text(json.dumps({"eventType": "input_received", "data": {
        "kind": "deployment_confirmation", "has_images": True, "selected_value": "private-image-caption",
    }}), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["pending_step"] == "materialize_selected_candidate"
    assert facts["pending_input_kind"] == "ask_user_question"
    assert facts["pending_question_answered"] is False
    assert facts["a2a_event_counts"] == {"input_received": 1, "confirmation_free_text": 1, "confirmation_image": 1}
    assert "private-" not in json.dumps(facts)


def test_diagnostics_normalize_numbered_waits_and_count_missing_or_unexpected_stack_names(tmp_path: Path) -> None:
    (tmp_path / "owned-stack-names.json").write_text(json.dumps(["private-owned-name"]), encoding="utf-8")
    (tmp_path / "cleanup-result.json").write_text(json.dumps({"resources": [
        {"stackId": "private-id1", "stackName": ""},
        {"stackId": "private-id2", "stackName": "private-unexpected-name"},
        {"stackId": "private-id3", "stackName": "private-owned-name"},
    ]}), encoding="utf-8")
    (tmp_path / "repl-events.jsonl").write_text(json.dumps({
        "type": "expect", "passed": False, "description": "Step 2 parameter ask #1 input ready",
    }), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["failed_wait"] == "Step 2 parameter question input ready"
    assert facts["cleanup_missing_name_count"] == 1
    assert facts["cleanup_unexpected_name_count"] == 1
    assert "private-" not in json.dumps(facts)
    (tmp_path / "repl-events.jsonl").unlink()
    facts = collect_live_diagnostics(tmp_path, {"error": (
        "TimeoutError: timed out waiting for deployment confirmation selector ready #1"
    )})
    assert facts["failed_wait"] == "deployment confirmation selector ready"
    assert "failed_wait" not in collect_live_diagnostics(tmp_path, {"error": (
        "TimeoutError: timed out waiting for Step 2 parameter ask #1 input ready private-key"
    )})


def test_completion_diagnostics_export_only_fixed_codes_and_validators(tmp_path):
    meta = tmp_path / 'pipeline' / 'meta.yaml'
    meta.parent.mkdir()
    meta.write_text(yaml.safe_dump({'status': 'failed', 'current_step': 'solution_planning_and_selection',
        'reason': 'Schema validation failed private-secret', 'normal_handoff': {'status': 'failed'}}), encoding="utf-8")
    transcript = meta.parent / 'transcripts' / 'step1' / 'session.jsonl'
    transcript.parent.mkdir(parents=True)
    rows = [
        {'content': [{'type': 'tool_use', 'name': 'read_file', 'id': 'doc'},
                     {'type': 'tool_use', 'name': 'complete_step', 'id': 'complete'}]},
        {'content': [{'type': 'tool_result', 'tool_use_id': 'doc', 'is_error': True,
                      'content': 'conclusion_schema_validation_failed example'},
                     {'type': 'tool_result', 'tool_use_id': 'complete', 'is_error': True,
                      'content': 'completion_input_schema_validation_failed {"validator":"required",'
                                 '"received":"private-secret"}'}]},
    ]
    transcript.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['pipeline_status'] == 'failed'
    assert facts['normal_handoff_status'] == 'failed'
    assert facts['completion_error_codes'] == {'input_schema': 1}
    assert facts['complete_step_error_count'] == 1
    assert facts['completion_schema_validators'] == ['required']
    assert 'private-secret' not in json.dumps(facts)


def test_cleanup_subprocess_diagnostics_export_only_known_fields(tmp_path):
    (tmp_path / 'cleanup-private-stack.log').write_text(json.dumps({'cleanupDiagnostic': {
        'stage': 'get_stack', 'status': 'DELETE_IN_PROGRESS', 'errorType': 'TimeoutError',
        'code': 'unknown', 'stackId': 'private-stack', 'message': 'private-secret',
    }}) + '\n' + json.dumps({'cleanupDiagnostic': {'stage': 'private-secret', 'status': 'private-stack'}}),
        encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['cleanup_attempt_diagnostics'] == [{
        'stage': 'get_stack', 'status': 'DELETE_IN_PROGRESS', 'errorType': 'TimeoutError', 'code': 'unknown'}]
    assert 'private-' not in json.dumps(facts)


def test_external_runtime_checkpoint_and_single_schema_error_are_projected_once(tmp_path):
    reports = tmp_path / 'reports'
    reports.mkdir()
    runtime = tmp_path / 'runtime'
    meta = runtime / 'projects/p/s/pipeline/meta.yaml'
    meta.parent.mkdir(parents=True)
    meta.write_text(yaml.safe_dump({'status': 'waiting_input', 'current_step': 'architecture_design',
        'execution': {'pending_input_kind': 'ask_user_question',
                      'pending_ask_user_question_input': {'question': 'private-question'}}}), encoding='utf-8')
    transcript = meta.parent / 'transcripts/step/session.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step'},
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
         'content': "'hard_constraints' is a required property; 'private-secret' is a required property "
                    '{"path":"/candidates/private-id/resource_intents","validator":"type"}'}
    ]}) + '\n', encoding='utf-8')
    facts = collect_live_diagnostics(reports, {}, runtime_config_dir=runtime)
    assert facts['pending_step'] == 'architecture_design'
    assert facts['pending_input_kind'] == 'ask_user_question'
    assert facts['complete_step_error_count'] == 1
    assert facts['completion_schema_validators'] == ['required', 'type']
    assert facts['completion_schema_missing_fields'] == ['hard_constraints']
    assert facts['completion_schema_fields'] == ['candidates', 'resource_intents']
    assert 'private-' not in json.dumps(facts)
    same_roots = collect_live_diagnostics(tmp_path, {}, runtime_config_dir=runtime)
    assert same_roots['complete_step_error_count'] == 1


def test_content_blocks_preserve_validation_details_without_python_repr_escaping(tmp_path):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'content': [
        {'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step'},
        {'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
         'content': [{'type': 'text', 'text': json.dumps({
             'error': 'conclusion_schema_validation_failed', 'path': '/candidates/private-id',
             'validator': 'required', 'message': "'hard_constraints' is a required property"})}]}
    ]}) + '\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_schema_missing_fields'] == ['hard_constraints']
    assert facts['completion_schema_fields'] == ['candidates']
    assert facts['completion_schema_validators'] == ['required']
    assert 'private' not in json.dumps(facts)


def test_network_sidecar_exports_only_fixed_fields(tmp_path):
    (tmp_path / '.e2e-network-fixture-diagnostic.json').write_text(json.dumps({
        'network_fixture_failure_category': 'throttled', 'network_fixture_exit_code': 1,
        'network_fixture_known_codes': ['Throttling', 'private-code'],
        'network_fixture_scan_retry_count': 2, 'network_fixture_scan_retry_code': 'StackNotFound',
        'stderr': 'private credential',
    }), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['network_fixture_known_codes'] == ['Throttling']
    assert facts['network_fixture_failure_category'] == 'throttled'
    assert facts['network_fixture_scan_retry_count'] == 2
    assert 'private' not in json.dumps(facts)


def test_cloud_action_diagnostics_export_only_known_action_names(tmp_path):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'aliyun_api',
             'input': {'action': 'CreateStack', 'params': {'secret': 'private'}}},
            {'type': 'tool_use', 'name': 'aliyun_api', 'input': {'action': 'private-action'}},
            {'type': 'tool_use', 'name': 'bash', 'input': {'command': 'client.create_v_switch(private_secret)'}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_use', 'name': 'aliyun_api', 'input': {'action': 'CreateStack'}}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['cloud_api_action_counts'] == {'CreateStack': 1, 'bash:CreateVSwitch': 1}
    assert 'private' not in json.dumps(facts)


def test_stream_diagnostics_exports_only_fixed_non_secret_fields(tmp_path):
    (tmp_path / 'stream-diagnostics.jsonl').write_text(json.dumps({
        'outcome': 'error', 'last_state': 'TASK_STATE_WORKING', 'elapsed_seconds': 300.2,
        'event_count': 42, 'jsonrpc_error_code': -32603, 'response_content_type': 'text/event-stream',
        'prompt': 'private', 'error': 'private cloud payload', 'task_id': 'private-id',
    }) + '\n' + json.dumps({'outcome': 'secret', 'last_state': 'private', 'event_count': True}) + '\n',
        encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['stream_diagnostics'] == [{
        'outcome': 'error', 'last_state': 'TASK_STATE_WORKING', 'elapsed_seconds': 300.2,
        'event_count': 42, 'jsonrpc_error_code': -32603, 'response_content_type': 'text/event-stream',
    }]
    assert 'private' not in json.dumps(facts)


def test_server_traceback_projects_source_frames_and_types_without_messages(tmp_path):
    (tmp_path / 'server-0.stderr.log').write_text(
        'Traceback (most recent call last):\n'
        '  File "/private/config/secret-key/iac_code/a2a/app.py", line 591, in get_pipeline_state\n'
        '  File "/private/sdk.py", line 12, in request\n'
        'TypeError: Object of type PrivateCredential is not JSON serializable: secret-value\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['server_exception_types'] == {'TypeError': 1}
    assert facts['server_source_frames'] == ['src/iac_code/a2a/app.py:591']
    assert facts['server_exception_categories'] == {'json_serialization': 1}
    assert not any(value in json.dumps(facts) for value in ('PrivateCredential', 'secret', '/private/', 'sdk.py'))


def test_natural_handoff_failure_diagnostic_retains_only_real_fixed_blocker_kinds(tmp_path):
    (tmp_path / 'server-0.stderr.log').write_text(
        'A2A natural handoff unavailable: phase=running status=completed '
        'blocker_counts={"agent_loop": 1, "secret-user-input": 99}\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['server_exception_categories'] == {
        'natural_handoff_phase:running': 1, 'natural_handoff_status:completed': 1,
        'natural_handoff_blocker:agent_loop': 1,
    }
    assert 'secret' not in json.dumps(facts)


@pytest.mark.parametrize(('message', 'category'), [
    ('Invalid A2A workspace metadata. private-path', 'workspace_metadata'),
    ('Current model private-model does not support image input.', 'model_image_unsupported'),
    ('当前模型 private-model 不支持图片输入。', 'model_image_unsupported'),
    ('A2A file URL part is outside the allowed workspace. private-path', 'image_part_invalid'),
])
def test_rpc_rejection_projects_fixed_request_category_without_private_message(tmp_path, message, category):
    (tmp_path / 'initial.events.jsonl').write_text(json.dumps({
        'error': {'code': -32602, 'message': message},
    }), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['jsonrpc_request_error_categories'] == [category]
    assert 'private' not in json.dumps(facts)


def test_pty_traceback_diagnostic_projects_real_message_origin_without_relaxing_verdict(tmp_path):
    traceback = (
        'Traceback (most recent call last):\n'
        '  File "/private/home/iac_code/a2a/app.py", line 591, in run\n'
        '  File "/private/sdk.py", line 12, in request\n'
        'ValueError: private-cloud-body\n'
    )
    (tmp_path / 'transcript.normalized.log').write_text(traceback, encoding='utf-8')
    transcript = tmp_path / 'transcripts' / 'fixture' / 'session.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'role': 'user', 'content': [
        {'type': 'tool_result', 'content': traceback, 'is_error': True},
    ]}), encoding='utf-8')
    checks = {'acceptance: no terminal error in PTY transcript': False}
    facts = collect_live_diagnostics(tmp_path, {'checks': checks})
    assert facts['pty_terminal_error_markers'] == {'traceback': 1}
    assert facts['pty_exception_types'] == {'ValueError': 1}
    assert facts['pty_source_frames'] == ['src/iac_code/a2a/app.py:591']
    assert facts['pty_terminal_marker_message_origins'] == {'tool_result:traceback': 1}
    assert checks['acceptance: no terminal error in PTY transcript'] is False
    assert not any(value in json.dumps(facts) for value in ('private', 'sdk.py'))


def test_native_terminal_facts_keep_failure_flags_before_handoff_without_private_payload(tmp_path):
    display = tmp_path / 'pipeline' / 'display.jsonl'
    display.parent.mkdir()
    display.write_text('\n'.join(json.dumps(row) for row in [
        {'type': 'step_failed', 'step_id': 'architecture_planning', 'payload': {
            'error': 'No conclusion extracted (agent stop reason: stream_error) private-response',
            'error_details': {'type': 'StepFailed', 'secret': 'private-key'},
        }},
        {'type': 'pipeline_completed', 'step_id': 'architecture_planning', 'payload': {
            'failed': True, 'early_exit': False, 'cloud': 'private-resource'},
        },
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['native_pipeline_terminal_events'] == [
        {'type': 'step_failed', 'step': 'architecture_planning',
         'reason_codes': ['no_conclusion', 'model_stream_error'], 'error_type': 'StepFailed'},
        {'type': 'pipeline_completed', 'step': 'architecture_planning', 'failed': True, 'early_exit': False},
    ]
    assert 'private' not in json.dumps(facts)


def test_repl_runtime_logs_export_qualified_provider_error_without_response(tmp_path):
    config = tmp_path / 'isolated-config'
    log = config / 'logs' / 'fixture.log'
    log.parent.mkdir(parents=True)
    log.write_text(
        'Traceback (most recent call last):\n'
        '  File "/private/host/iac_code/providers/dashscope_provider.py", line 145, in request\n'
        'openai.BadRequestError: invalid_parameter_error private-key private-cloud-response\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path / 'runner', {}, runtime_config_dir=config)
    assert facts['server_exception_types'] == {'BadRequestError': 1}
    assert facts['server_exception_categories'] == {'model_bad_request': 1}
    assert facts['server_source_frames'] == ['src/iac_code/providers/dashscope_provider.py:145']
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize(('body', 'category'), [
    ('All candidates in candidateSetId=private-batch already have rich details.', 'details_already_complete'),
    ('show_candidate_detail candidate_index=1 is not allowed yet; expected candidate_index=0, private-name',
     'index_or_name_mismatch'),
    ('Failed to render the candidate topology: private-node', 'invalid_topology'),
])
def test_candidate_detail_errors_export_fixed_categories_without_batch_names_or_graph(tmp_path, body, category):
    path = tmp_path / 'pipeline/transcripts/transcript_att_0001/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'show_candidate_detail', 'id': 'private-call'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-call',
                                    'is_error': True, 'content': body}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['candidate_detail_error_categories'] == {category: 1}
    assert 'private' not in json.dumps(facts)


def test_provider_warning_diagnostic_handles_no_traceback_but_never_cloud_text(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'native.log').write_text(
        'WARNING iac_code.providers.manager:_consume: Streaming failed, falling back to non-streaming: '
        'RateLimitError private-token private-resource\n'
        'INFO tool_result: documentation says RateLimitError private-token\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['provider_failure_categories'] == {'rate_limit': 1}
    assert 'private' not in json.dumps(facts)


def test_provider_failure_event_in_server_log_keeps_fixed_categories_only(tmp_path):
    (tmp_path / 'server-restarted.stdout.log').write_text(
        "INFO iac_code.services.telemetry.sink:emit: [event] iac.api.request.failed "
        "{'error_type': 'BadRequestError', 'error_message': 'InvalidParameterRange: "
        "max_tokens out of range private-key private-resource'}\n"
        "INFO tool_result: InvalidParameterRange max_tokens private-key\n", encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['provider_failure_categories'] == {'bad_request': 1, 'parameter_range': 1}
    assert facts['provider_failure_types'] == {'BadRequestError': 1}
    assert facts['provider_failure_fields'] == {'max_tokens': 1}
    assert 'private' not in json.dumps(facts)


def test_agui_provider_warning_in_native_server_stderr_is_collected(tmp_path):
    (tmp_path / 'a2a.stderr.log').write_text(
        'WARNING iac_code.providers.manager:_consume: Streaming failed: '
        'APIConnectionError private-key\n', encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['provider_failure_categories'] == {'connection': 1}
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize('step', [1, 2, 3])
def test_early_stream_end_retains_exact_rollback_checkpoint(tmp_path, step):
    facts = collect_live_diagnostics(tmp_path, {'abort_reason': (
        f'RuntimeError: private-stream ended before rollback Step {step} started: private payload')})
    assert facts['failed_wait'] == f'rollback Step {step} started'
    assert facts['abort_category'] == 'stream_ended_before_checkpoint'
    assert 'private' not in json.dumps(facts)
    unknown = collect_live_diagnostics(tmp_path, {'error': 'RuntimeError: private ended before private checkpoint'})
    assert 'failed_wait' not in unknown


def test_stream_transport_diagnostics_keep_only_fixed_categories(tmp_path):
    (tmp_path / 'stream-diagnostics.jsonl').write_text('\n'.join(json.dumps(row) for row in [
        {'outcome': 'error', 'error_kind': 'timeout', 'http_status': 504, 'error': 'private'},
        {'error_kind': 'private', 'http_status': True},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['stream_diagnostics'] == [{'outcome': 'error', 'error_kind': 'timeout', 'http_status': 504}]
    assert 'private' not in json.dumps(facts)


def test_materialization_rejections_and_unreturned_bash_are_retained_without_bodies(tmp_path):
    path = tmp_path / 'transcripts' / 'transcript_att_0001' / 'session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'private-1', 'name': 'complete_step', 'input': {}},
            {'type': 'tool_use', 'id': 'private-2', 'name': 'bash', 'input': {'command': 'python3 private.py'}},
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'private-1', 'is_error': True,
             'content': 'validate the authoritative candidate output_path after its latest write: private'},
        ]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_error_codes'] == {'materialize_validation_stale': 1}
    assert facts['bash_tool_trace'] == [{'step': 'unknown', 'kind': 'python', 'has_result': False}]
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize('resource,status,expected', [
    ({'Success': True, 'Result': {}}, 'unavailable',
     {'resource_order_missing': 1, 'resource_supplement_missing': 1,
      'resource_supplement_missing_product_other': 1,
      'resource_supplement_missing_invalid_amount': 1}),
    ({'Success': False}, 'unavailable',
     {'resource_marked_failed': 1, 'resource_result_missing': 1,
      'failed_resource_product_other': 1, 'failed_resource_error_absent': 1}),
    ({'Success': True, 'Result': {'Order': {'TradeAmount': 12.34, 'Currency': 'CNY'},
                                'OrderSupplement': {'PriceUnit': '/hour'}}}, 'succeeded',
     {'resource_amount_present': 1, 'resource_currency_cny': 1, 'resource_priceunit_present': 1}),
])
def test_quote_diagnostic_distinguishes_native_price_projection_without_exporting_response(
    tmp_path, resource, status, expected,
):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    body = json.dumps({'Resources': {'private-resource': resource}, 'private': 'private-secret'})
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'private-id',
                                        'name': 'ros_estimate_template_cost'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-id',
                                    'is_error': False, 'content': body + '\n---\nROS preflight\nprivate-secret'}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['quote_response_diagnostics'] == {'native_result_projection_' + status: 1, **expected}
    assert facts['quote_native_result_shapes'] == {'non_json_text': 1}
    assert 'private' not in json.dumps(facts) and '12.34' not in json.dumps(facts)


def test_unpriced_quote_retains_product_and_possible_alternate_period_field_presence_only(tmp_path):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    resource = {'Success': True, 'Type': 'ALIYUN::VPC::EIP',
                'Result': {'Order': {'TradeAmount': 12.34, 'Currency': 'CNY', 'Period': 'private'}},
                'Properties': {'InternetChargeType': 'private', 'PeriodUnit': 'private'}}
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'ros_estimate_template_cost', 'id': 'private'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private', 'is_error': False,
                                     'content': json.dumps({'Resources': {'private': resource}})}]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding="utf-8")
    diagnostic = collect_live_diagnostics(tmp_path, {})['quote_response_diagnostics']
    assert diagnostic['native_result_projection_unavailable'] == 1
    assert diagnostic['resource_supplement_missing_product_eip'] == 1
    assert diagnostic['resource_supplement_missing_order_period_present'] == 1
    assert diagnostic['resource_supplement_missing_properties_periodunit_present'] == 1
    assert diagnostic['resource_supplement_missing_properties_internetchargetype_present'] == 1
    assert not any(s in json.dumps(diagnostic) for s in ('private', '12.34'))


@pytest.mark.parametrize('error_field', ['Error', 'ErrorMessage', 'Result'])
def test_failed_quote_resource_keeps_native_error_category_without_response_body(tmp_path, error_field):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    error = {'Code': 'MissingParameter', 'Message': 'MissingParameter DBInstanceClass private-secret'}
    resource = {'Success': False, 'Type': 'ALIYUN::RDS::DBInstance',
                error_field: {'Error': error} if error_field == 'Result' else error,
                'Properties': {'DBInstanceClass': 'private-class'}, 'private': 'private-secret'}
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'native-quote',
                                         'name': 'ros_estimate_template_cost'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'native-quote',
                                     'is_error': False, 'content': json.dumps({
                                         'Resources': {'private-resource': resource}})}]},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    diagnostic = facts['quote_response_diagnostics']
    assert diagnostic['native_result_projection_unavailable'] == 1
    assert diagnostic['failed_resource_product_rds'] == 1
    assert diagnostic['failed_resource_error_missing_parameter'] == 1
    assert diagnostic['failed_resource_error_field_DBInstanceClass'] == 1
    assert 'private' not in json.dumps(facts)
    resource['Success'] = True
    resource['Result'] = {'Order': {'TradeAmount': 12.34, 'Currency': 'CNY'},
                          'OrderSupplement': {'PriceUnit': '/hour'}}
    rows[-1]['content'][0]['content'] = json.dumps({'Resources': {'private-resource': resource}})
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['quote_response_diagnostics']['native_result_projection_succeeded'] == 1
    assert not any(key.startswith('failed_resource_') for key in facts['quote_response_diagnostics'])


@pytest.mark.parametrize('amounts,category', [
    ({'OriginalAmount': '0', 'TradeAmount': 0}, 'zero_amount'),
    ({'TradeAmount': '0.00'}, 'zero_amount'),
    ({'OriginalAmount': '12.34', 'TradeAmount': 0}, 'nonzero_amount'),
    ({'TradeAmount': 'private-secret'}, 'invalid_amount'),
    ({'TradeAmount': True}, 'invalid_amount'),
    ({'TradeAmount': 'NaN'}, 'invalid_amount'),
    ({'TradeAmount': 'Infinity'}, 'invalid_amount'),
    ({'TradeAmount': -1}, 'invalid_amount'),
    ({}, 'invalid_amount'),
])
def test_missing_quote_billing_basis_keeps_only_amount_category(tmp_path, amounts, category):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    body = {'Resources': {'private-resource': {'Success': True, 'Result': {'Order': amounts}}}}
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'private-id',
                                        'name': 'ros_estimate_template_cost'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-id',
                                    'is_error': False, 'content': json.dumps(body)}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['quote_response_diagnostics']['resource_supplement_missing_' + category] == 1
    assert 'private' not in json.dumps(facts) and '12.34' not in json.dumps(facts)


def test_native_repl_input_trace_retains_order_without_user_text_or_parameter_values(tmp_path):
    path = tmp_path / 'pipeline/display.jsonl'
    path.parent.mkdir(parents=True)
    step = 'materialize_selected_candidate'
    rows = [
        {'type': 'user_input_required', 'step_id': step, 'payload': {
            'kind': 'deployment_confirmation', 'solution_summary': 'private-first',
            'effective_deployment_parameters': {'private': 'private-first'}}},
        {'type': 'user_input_received', 'step_id': step, 'payload': {
            'kind': 'deployment_confirmation', 'structured': False,
            'selected_value': '确认部署，参数覆盖保持刚才的值 private-secret'}},
        {'type': 'user_input_required', 'step_id': step, 'payload': {
            'kind': 'deployment_confirmation', 'solution_summary': 'private-second',
            'effective_deployment_parameters': {'private': 'private-second'}}},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    trace = facts['native_repl_input_traces'][0]
    assert [row['index'] for row in trace] == [0, 1, 2]
    assert trace[1]['structured'] is False
    assert trace[1]['confirmation_word_present'] and trace[1]['adjustment_word_present']
    assert trace[2]['confirmation_changed'] is True
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize('key,category', [
    ('solution_first_confirmed_template_validated', 'confirmation_template_guard'),
    ('solution_first_confirmation_wait_required', 'confirmation_wait_guard'),
    ('solution_first_revalidate_after_template_write', 'confirmation_template_mutated'),
    ('hard_constraint_verification_required', 'hard_constraint_guard'),
])
def test_native_confirmation_error_is_correlated_to_status_without_private_tool_payload(tmp_path, key, category):
    from iac_code.pipeline.engine.complete_step_tool import _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY

    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step',
            'input': {'conclusion': {'status': 'confirmed', 'private': 'private-secret'}}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
            'content': _COMPLETION_GUARD_MESSAGE_TEXT_BY_KEY[key] + ' private-secret'}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_error_codes'][category] == 1
    assert facts['completion_error_decisions'] == [
        {'step': 'unknown', 'status': 'confirmed', 'categories': [category]}]
    assert 'private' not in json.dumps(facts)


@pytest.mark.parametrize('text,category', [
    ('确认结论必须指向 ros_validate_template 最后一次校验通过的模板文件。', 'confirmation_template_guard'),
    ('只有当前方案已在专用确认状态中展示后，才能确认部署。', 'confirmation_wait_guard'),
    ('确认使用的模板在 ros_validate_template 之后被改写。', 'confirmation_template_mutated'),
    ('每个用户明确提出的硬约束都必须由一条状态为满足的检查覆盖，且参数和证据一致。', 'hard_constraint_guard'),
])
def test_localized_native_confirmation_guard_error_keeps_fixed_category(tmp_path, text, category):
    path = tmp_path / 'pipeline/transcripts/step/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(row) for row in [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'private-id', 'name': 'complete_step',
            'input': {'conclusion': {'status': 'confirmed'}}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-id', 'is_error': True,
            'content': text + ' private-secret'}]},
    ]), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['completion_error_codes'][category] == 1
    assert facts['completion_error_decisions'][0]['categories'] == [category]
    assert 'private' not in json.dumps(facts)


def test_constraint_diagnostics_use_rejected_input_and_preserve_only_shapes_and_codes(tmp_path):
    path = tmp_path / "pipeline/transcripts/step/session.jsonl"
    path.parent.mkdir(parents=True)
    checks = [{"constraint_id": "private-id", "status": "unresolved", "actual_value": ["private-secret"],
               "parameter_values": {"password": "private-secret"}, "evidence": [{"secret": "private"}]}]
    path.write_text("\n".join(json.dumps(row) for row in [
        {"role": "assistant", "content": [{"type": "tool_use", "name": "complete_step", "id": "private-call",
         "input": {"conclusion": {"status": "confirmed", "hard_constraint_checks": checks}}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "private-call", "is_error": True,
         "content": "Every explicit user hard constraint must be covered. "
                    "constraint_not_satisfied[private-id]; constraint_comparison_failed[private-id]"}]},
    ]), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["completion_failed_constraint_inputs"] == [{"source": "rejected_tool_input", "step": "unknown",
            "input_location": "conclusion", "issues": ["constraint_comparison_failed", "constraint_not_satisfied"],
            "actual_type": "array", "llm_status": "unresolved", "parameter_values_count": 1, "evidence_count": 1}]
    assert "private" not in json.dumps(facts) and "password" not in json.dumps(facts)
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["content"][0]["input"]["conclusion"][
        "hard_constraint_checks"] == checks


def test_sparse_confirmation_diagnostic_does_not_claim_saved_checks_are_rejected_inputs(tmp_path):
    path = tmp_path / "pipeline/transcripts/step/session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in [
        {"role": "assistant", "content": [{"type": "tool_use", "name": "complete_step", "id": "private-call",
         "input": {"conclusion": {"status": "confirmed"}}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "private-call", "is_error": True,
         "content": "Every explicit user hard constraint must be covered. "
                    "constraint_not_satisfied[private-id]; constraint_comparison_failed[private-id]"}]},
    ]), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["completion_failed_constraint_inputs"] == [{
        "source": "rejected_tool_input", "step": "unknown", "checks_supplied_count": 0,
        "checks_omitted": True, "matched_check_count": 0,
        "issues": ["constraint_comparison_failed", "constraint_not_satisfied"],
    }]
    assert "private" not in json.dumps(facts)


def test_constraint_delta_trace_keeps_network_hashes_and_native_check_order_only(tmp_path):
    import hashlib

    path = tmp_path / "pipeline/transcripts/step/session.jsonl"
    path.parent.mkdir(parents=True)
    checks = [{"constraint_id": "private-id", "status": "conflict", "actual_value": "10.1.1.0/24",
               "constraint": {"value": "10.1.0.0/24", "source_text": "private-secret"}}]
    path.write_text(json.dumps({"role": "assistant", "content": [{
        "type": "tool_use", "name": "complete_step", "id": "private-call",
        "input": {"conclusion": {"status": "awaiting_confirmation", "hard_constraint_checks": checks}},
    }]}), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts["completion_decision_inputs"][0]["submitted_constraint_checks"] == [{
        "input_location": "conclusion", "check_index": 0, "llm_status": "conflict",
        "actual_cidr_hash": hashlib.sha256(b"10.1.1.0/24").hexdigest(),
        "expected_cidr_hash": hashlib.sha256(b"10.1.0.0/24").hexdigest(),
    }]
    assert "private" not in json.dumps(facts) and "10.1." not in json.dumps(facts)


@pytest.mark.parametrize("suffix,kind", [("a1b2c3","example_or_placeholder"), ("20261008","date_only"),
                                        ("d12ebd907184","alphanumeric")])
def test_deployment_name_conflict_diagnostic_correlates_rejected_call_without_names(tmp_path, suffix, kind):
    path = tmp_path / "pipeline/transcripts/step/session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in [
        {"role":"assistant","content":[{"type":"tool_use","name":"ros_deploy","id":"private-call",
         "input":{"action":"create","stack_name":"private-name-"+suffix}}]},
        {"role":"user","content":[{"type":"tool_result","tool_use_id":"private-call","is_error":True,
         "content":"StackExists private-name private-token"}]},
    ]), encoding="utf-8")
    facts = collect_live_diagnostics(tmp_path,{})
    trace = facts["deployment_input_identity_trace"][0]
    assert trace["name_suffix_kind"] == kind
    assert trace["has_result"] is True and trace["is_error"] is True
    assert trace["result_category"] == "already_exists"
    assert "private" not in json.dumps(facts) and suffix not in json.dumps(facts)


@pytest.mark.parametrize('message', [
    'The parameter VpcId is not defined in the template. private-secret',
    'ParameterNotFound private-secret VpcId',
    '参数VpcId未声明 private-secret',
])
def test_cloud_error_diagnostic_identifies_undeclared_parameter_without_values(tmp_path, message):
    from scripts.ci.live_diagnostics import _cloud_tool_failure_facts
    path = tmp_path / 'transcripts/attempt/session.jsonl'
    path.parent.mkdir(parents=True)
    rows = [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'ros_deploy', 'id': 'private-call'}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'private-call',
                                    'is_error': True, 'content': message}]},
    ]
    path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
    facts = _cloud_tool_failure_facts(tmp_path, None)
    assert facts['cloud_tool_error_categories'] == {'undeclared_parameter': 1}
    assert facts['cloud_tool_error_by_tool'] == {'ros_deploy:undeclared_parameter': 1}
    assert 'private' not in json.dumps(facts)
