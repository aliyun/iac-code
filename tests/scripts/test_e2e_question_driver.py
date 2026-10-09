from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

from scripts import e2e_question_driver as driver


@pytest.mark.parametrize(('question', 'keys', 'expected'), [
    ('请选择可用区 ZoneId', ['zone_id'], 'cn-hangzhou-i'),
    ('现在请提供 VpcId', ['vpc_id'], 'vpc-fixture'),
    ('这个产品是什么用途？', ['goal'], '只创建安全组'),
])
def test_answers_are_grounded_in_current_question_not_turn_order(tmp_path, monkeypatch, question, keys, expected):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'fact_keys': keys, 'option_id': ''})
    diagnostics = {}
    answer, category = driver.answer_question(tmp_path, {'question': question},
        {'goal': '只创建安全组，不创建 VSwitch，本轮不部署', 'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i'},
        {}, diagnostics)
    assert expected in answer
    assert '不创建 VSwitch，本轮不部署' in answer
    assert category in {'goal', 'zone_id', 'vpc_id'}
    assert diagnostics['question_driver_llm_count'] == 1


def test_helper_cannot_invent_ids_or_remove_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['vpc-attacker'], 'option_id': '', 'answer': '确认部署 vpc-invented'})
    answer, _ = driver.answer_question(tmp_path, {'question': 'VpcId?'},
        {'goal': '只询价，不部署', 'vpc_id': 'vpc-fixture'}, {}, {})
    assert answer == '只询价，不部署；vpc-fixture'
    assert 'attacker' not in answer and 'invented' not in answer


def test_required_parameters_are_answered_separately_even_on_helper_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: None)
    facts = {'goal': '逐项询问，不部署', 'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i'}
    counts = {}
    for question, expected, absent in [('VpcId?', 'vpc-fixture', 'cn-hangzhou-i'),
                                      ('ZoneId 可用区?', 'cn-hangzhou-i', 'vpc-fixture')]:
        answer, _ = driver.answer_question(tmp_path, {'question': question, 'one_parameter_at_a_time': True},
                                           facts, counts, {})
        assert expected in answer and absent not in answer


@pytest.mark.parametrize('helper_available', [False, True])
def test_current_zone_question_does_not_reanswer_vpc_from_background(tmp_path, monkeypatch, helper_available):
    calls = []

    def select(_config, pending, _facts):
        calls.append(pending)
        if not helper_available:
            return None
        return {'fact_keys': ['goal', 'zone_id'] if pending.get('_fact_selection_review')
                else ['goal', 'vpc_id', 'zone_id'], 'missing_fields': []}

    monkeypatch.setattr(driver, '_select_facts', select)
    facts = {'goal': '逐项提供 VpcId 和 ZoneId，本轮不部署', 'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i'}
    pending = {'question': '已收到 VpcId，当前需要选择 ZoneId 可用区。', 'one_parameter_at_a_time': True,
               'options': [{'id': 'zone-a', 'label': 'cn-hangzhou-a'},
                           {'id': 'zone-i', 'label': 'cn-hangzhou-i'}]}
    answer, category = driver.answer_question(tmp_path, pending, facts, {}, {})
    assert 'cn-hangzhou-i' in answer and 'vpc-fixture' not in answer and category == 'zone_id'


@pytest.mark.parametrize('review', [None, {'fact_keys': ['goal', 'vpc_id', 'zone_id']},
                                   {'fact_keys': ['goal', 'zone_id'], 'missing_fields': []}])
def test_ambiguous_single_parameter_requires_grounded_helper_review(tmp_path, monkeypatch, review):
    calls = []

    def select(_config, pending, _facts):
        calls.append(pending)
        return review if len(calls) == 2 else {'fact_keys': ['goal', 'vpc_id', 'zone_id']}

    monkeypatch.setattr(driver, '_select_facts', select)
    facts = {'goal': '逐项提供参数，不部署', 'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i'}
    pending = {'question': '上次 VpcId 已提供，这次 ZoneId 如何设置？', 'one_parameter_at_a_time': True}
    diagnostics = {}
    if review is None or len(review['fact_keys']) == 3:
        with pytest.raises(RuntimeError, match='cannot ground the current single identity parameter'):
            driver.answer_question(tmp_path, pending, facts, {}, diagnostics)
    else:
        answer, category = driver.answer_question(tmp_path, pending, facts, {}, diagnostics)
        assert 'cn-hangzhou-i' in answer and 'vpc-fixture' not in answer and category == 'zone_id'
        assert diagnostics['question_driver_parameter_zone_id_count'] == 1
    assert len(calls) == 2 and calls[1]['_fact_selection_review']['issue'] == 'current_parameter'
    assert diagnostics['question_driver_parameter_review_count'] == 1


def test_repeated_questions_have_a_hard_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'fact_keys': ['goal']})
    counts, diagnostics = {}, {}
    for _ in range(3):
        driver.answer_question(tmp_path, {'question': '用途？'}, {'goal': '测试应用，仅规划'}, counts, diagnostics)
    with pytest.raises(RuntimeError, match='budget exhausted'):
        driver.answer_question(tmp_path, {'question': '用途？'}, {'goal': '测试应用，仅规划'}, counts, diagnostics)
    assert diagnostics['question_driver_budget_exhausted'] is True


@pytest.mark.parametrize('option', ['missing', 'deploy'])
def test_helper_cannot_select_nonexistent_or_control_options(tmp_path, monkeypatch, option):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'fact_keys': ['goal'], 'option_id': option})
    with pytest.raises(RuntimeError, match='allowed option'):
        driver.answer_question(tmp_path, {'question': '选择?', 'allowFreeText': False,
            'options': [{'id': 'deploy', 'label': '确认部署'}]}, {'goal': '只规划，不部署'}, {}, {})


def test_option_answer_uses_an_actual_transport_option_id(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'fact_keys': ['zone_id'], 'option_id': 'zone-1'})
    answer, category = driver.answer_question(tmp_path, {'question': '可用区?', 'allowFreeText': False,
        'options': [{'id': 'zone-1', 'label': '杭州可用区 i'}]}, {'zone_id': 'cn-hangzhou-i'}, {}, {})
    assert (answer, category) == ('zone-1', 'option')


def test_helper_uses_bailian_low_thinking_without_sending_credentials(tmp_path, monkeypatch):
    (tmp_path / '.credentials.yml').write_text('dashscope: sk-fixture-secret\n', encoding="utf-8")
    monkeypatch.delenv('IAC_CODE_E2E_DIAGNOSIS_LOCK', raising=False)
    def post(url, **kwargs):
        assert kwargs['json']['model'] == 'glm-5.3-prime'
        assert kwargs['json']['reasoning_effort'] == 'low'
        assert kwargs['timeout'] == 30
        assert 'sk-fixture-secret' not in json.dumps(kwargs['json'])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'choices': [{'message': {'content': '{"fact_keys":["goal"],"option_id":""}'}}]})
    monkeypatch.setattr(driver.httpx, 'post', post)
    answer, _ = driver.answer_question(tmp_path, {'question': 'API key sk-fixture-secret 用途?'},
                                       {'goal': '仅测试网络'}, {}, {})
    assert answer == '仅测试网络'


@pytest.mark.parametrize('invalid', [
    {'missing_detail': 'unsupported-provider'}, {'missing_detail': {'private': 'value'}},
    {'question_type': 'unsupported-type'}, {'fact_keys': 'goal'}, {'fact_keys': ['invented']},
    {'missing_fields': 'other'}, {'missing_fields': ['invented']}, {'option_id': 'invented'},
])
def test_helper_contract_failure_is_not_an_unavailable_fact_verdict(tmp_path, monkeypatch, invalid):
    (tmp_path / '.credentials.yml').write_text('dashscope: sk-fake\n', encoding='utf-8')
    monkeypatch.delenv('IAC_CODE_E2E_DIAGNOSIS_LOCK', raising=False)
    decision = {'fact_keys': ['goal', 'cloud_vendor'], 'option_id': '',
                'missing_fields': ['other'], 'question_type': 'new', 'missing_detail': 'unknown', **invalid}
    monkeypatch.setattr(driver.httpx, 'post', lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {
            'choices': [{'message': {'content': json.dumps(decision)}}]},
    ))
    facts = {'goal': '只使用 AWS，不使用阿里云，也不生成或部署 ROS 模板', 'cloud_vendor': 'AWS'}
    assert driver._select_facts(tmp_path, {'question': '改为阿里云还是仍使用 AWS?'}, facts) is None
    diagnostics = {}
    answer, _ = driver.answer_question(tmp_path, {'question': '改为阿里云还是仍使用 AWS?'}, facts, {}, diagnostics)
    assert all(value in answer for value in facts.values())
    assert 'invented' not in answer and 'unsupported' not in answer and 'private' not in answer
    assert diagnostics['question_driver_facts_fallback_count'] == 1


def test_valid_helper_verdict_still_blocks_missing_required_resource_id(tmp_path, monkeypatch):
    (tmp_path / '.credentials.yml').write_text('dashscope: sk-fake\n', encoding='utf-8')
    monkeypatch.delenv('IAC_CODE_E2E_DIAGNOSIS_LOCK', raising=False)
    decision = {'fact_keys': ['goal'], 'option_id': '', 'missing_fields': ['vpc_id'],
                'question_type': 'new', 'missing_detail': 'resource_id'}
    monkeypatch.setattr(driver.httpx, 'post', lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {
            'choices': [{'message': {'content': json.dumps(decision)}}]},
    ))
    with pytest.raises(RuntimeError, match='unavailable case facts: vpc_id'):
        driver.answer_question(tmp_path, {'question': '请提供必填 VpcId'}, {'goal': '只规划网络'}, {}, {})


def test_native_ack_waits_for_same_question_to_be_consumed(tmp_path, monkeypatch):
    path = tmp_path / 'meta.yaml'
    question = {'toolUseId': 'question-1', 'question': 'VpcId?'}
    state = {'execution': {'pending_input_kind': 'ask_user_question',
                          'pending_ask_user_question_input': question}}
    path.write_text(yaml.safe_dump(state), encoding="utf-8")
    drains = []
    def drain():
        drains.append(True)
        if len(drains) == 3:
            state['execution']['pending_ask_user_question_input']['answer'] = {'free_text': 'vpc-fixture'}
            path.write_text(yaml.safe_dump(state), encoding="utf-8")
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    driver.wait_native_question_ack(path, 'question-1', drain)
    assert len(drains) == 3


def test_network_fixture_discovery_validates_fixed_fields_without_printing_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(driver.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i', 'cidr': '10.0.1.0/24'})))
    facts = driver.network_facts('python', {}, tmp_path, '10.250.1.0/24')
    assert facts['vpc_id'] == 'vpc-fixture'
    monkeypatch.setattr(driver.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='SECRET'))
    with pytest.raises(RuntimeError) as exc:
        driver.network_facts('python', {}, tmp_path, '10.250.1.0/24')
    assert 'SECRET' not in str(exc.value)


def test_case_facts_index_literal_constraints_and_runtime_values_without_invention():
    goal = '小团队 Node.js 电商 API 上线；只创建杭州 VSwitch；不部署 ECS；网段 10.0.1.0/24'
    facts = driver.case_facts(goal, {'vpc_id': 'vpc-fixture', 'cidr': '10.0.2.0/24', 'secret': 'private'})
    assert facts['goal'] == goal
    assert facts['region'] == '只创建杭州 VSwitch'
    assert 'Node.js' in facts['workload']
    assert '不部署 ECS' in facts['constraints']
    assert facts['vpc_id'] == 'vpc-fixture'
    assert facts['cidr'] == '10.0.2.0/24'
    assert 'secret' not in facts
    assert 'zone_id' not in facts
    assert driver.case_facts('只规划安全组').get('region') is None


def test_prefix_answer_is_derived_from_supplied_cidr_not_a_new_subnet(tmp_path, monkeypatch):
    facts = driver.case_facts('仅创建 VSwitch', {'cidr': '10.0.2.0/27'})
    assert facts['cidr_prefix'] == '27'
    assert 'cidr_prefix' not in driver.case_facts('创建 VSwitch', {'cidr': 'invalid'})
    assert 'cidr_prefix' not in driver.case_facts('创建 VSwitch')
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cidr_prefix'], 'missing_fields': [], 'missing_detail': 'cidr_prefix'})
    answer, _ = driver.answer_question(tmp_path, {'question': '前缀长度是多少?'}, facts, {}, {})
    assert '27' in answer
    assert '10.0.2.0/24' not in answer


def test_network_only_workload_fact_states_absence_without_inventing_business():
    facts = driver.case_facts('选择已有 VPC，创建一个 VSwitch')
    assert facts['workload'].startswith('未指定应用工作负载')
    assert 'workload' not in driver.case_facts('为 ECS 实例创建一个 VSwitch')
    assert 'Node.js' in driver.case_facts('为 Node.js 应用创建 VSwitch')['workload']


def test_conversation_carries_submitted_answers_and_only_acknowledges_matching_question(tmp_path, monkeypatch):
    context = driver.QuestionConversation()
    seen = []
    def choose(_config, pending, _facts):
        seen.append([dict(turn) for turn in pending['_conversation']])
        return {'fact_keys': ['purpose'], 'question_type': 'supplement'}
    monkeypatch.setattr(driver, '_select_facts', choose)
    facts = {'goal': '只规划，不部署', 'purpose': '小团队测试 API'}
    counts, diagnostics = {}, {}
    first = {'toolUseId': 'first', 'question': '用途?'}
    driver.answer_question(tmp_path, first, facts, counts, diagnostics, conversation=context)
    context.acknowledge({'toolUseId': 'different'})
    assert context.turns[0]['acknowledged'] is False
    driver.answer_question(tmp_path, {'toolUseId': 'second', 'question': '应用规模?'}, facts,
                           counts, diagnostics, conversation=context)
    assert seen[1][0]['answer'] == '只规划，不部署；小团队测试 API'
    assert seen[1][0]['acknowledged'] is False
    context.acknowledge(first)
    assert context.turns[0]['acknowledged'] is True
    assert diagnostics['question_driver_supplement_count'] == 2


def test_new_goal_clears_previous_target_history_but_keeps_total_question_budget(tmp_path, monkeypatch):
    context, counts, diagnostics = driver.QuestionConversation(), {}, {}
    history = []
    monkeypatch.setattr(driver, '_select_facts', lambda _c, p, _f:
                        history.append(list(p['_conversation'])) or {'fact_keys': ['goal']})
    driver.answer_question(tmp_path, {'question': '原需求?'}, {'goal': '创建 VSwitch'}, counts,
                           diagnostics, conversation=context)
    driver.answer_question(tmp_path, {'question': '新需求?'}, {'goal': '只创建安全组，不创建 VSwitch'}, counts,
                           diagnostics, conversation=context)
    assert history[-1] == []
    assert len(context.turns) == 1
    assert sum(counts.values()) == 2
    assert diagnostics['question_driver_goal_reset_count'] == 1


def test_explicit_missing_fact_fails_without_fabrication_or_exposing_model_text(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['vpc_id', 'sk-private-secret'], 'answer': 'vpc-invented'})
    diagnostics = {}
    with pytest.raises(RuntimeError, match='unavailable case facts') as error:
        driver.answer_question(tmp_path, {'question': '指定 VpcId?'}, {'goal': '创建 VSwitch'}, {}, diagnostics)
    assert diagnostics['question_driver_missing_fields'] == ['other', 'vpc_id']
    assert 'sk-private-secret' not in str(error.value) and 'vpc-invented' not in str(error.value)


def test_model_cannot_claim_a_supplied_fact_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['vpc_id'], 'question_type': [], 'missing_fields': ['vpc_id']})
    answer, _ = driver.answer_question(tmp_path, {'question': 'VpcId?'},
                                       {'goal': '只规划', 'vpc_id': 'vpc-fixture'}, {}, {})
    assert 'vpc-fixture' in answer


def test_question_history_is_bounded_and_does_not_change_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'fact_keys': ['goal']})
    context, counts = driver.QuestionConversation(), {}
    for index in range(driver.MAX_QUESTIONS):
        driver.answer_question(tmp_path, {'question': f'补充第{index}项?'}, {'goal': '只规划'}, counts, {},
                               conversation=context)
    assert len(context.turns) == 6
    with pytest.raises(RuntimeError, match='budget exhausted'):
        driver.answer_question(tmp_path, {'question': '另一项?'}, {'goal': '只规划'}, counts, {}, conversation=context)


def test_history_payload_is_redacted_without_sending_tool_ids(tmp_path, monkeypatch):
    (tmp_path / '.credentials.yml').write_text('dashscope: sk-fixture-secret\n', encoding='utf-8')
    context = driver.QuestionConversation(goal='仅规划', turns=[{
        'question_id': 'private-tool-id', 'question': '用途 sk-fixture-secret?',
        'answer': '测试 sk-fixture-secret', 'fact_keys': ['purpose'], 'acknowledged': True}])
    def post(_url, **kwargs):
        payload = json.loads(kwargs['json']['messages'][1]['content'])
        assert payload['submitted_answers'][0]['acknowledged'] is True
        assert 'private-tool-id' not in json.dumps(payload)
        assert 'sk-fixture-secret' not in json.dumps(payload)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            'choices': [{'message': {'content': '{"fact_keys":["purpose"],"question_type":"repeat"}'}}]})
    monkeypatch.setattr(driver.httpx, 'post', post)
    diagnostics = {}
    driver.answer_question(tmp_path, {'question': '还是什么用途?'}, {'goal': '仅规划', 'purpose': '测试'},
                           {}, diagnostics, conversation=context)
    assert diagnostics['question_driver_repeat_count'] == 1


def test_missing_fixture_is_resolved_only_after_helper_requests_it(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda _config, _pending, facts: {
        'fact_keys': ['constraints'], 'missing_fields': ['vpc_id'], 'question_type': 'new'})
    requested = []
    def resolve(fields):
        requested.append(fields)
        return {'vpc_id': 'vpc-fixture', 'goal': 'deploy everything', 'zone_id': 'unrequested-zone'}
    counts, diagnostics = {}, {}
    answer, category = driver.answer_question(tmp_path, {'question': 'VpcId?'},
        {'goal': '复用已有 VPC，本轮不部署', 'constraints': '本轮不部署'}, counts, diagnostics,
        fact_resolver=resolve)
    assert requested == [('vpc_id',)]
    assert 'vpc-fixture' in answer and '本轮不部署' in answer
    assert 'deploy everything' not in answer and 'unrequested-zone' not in answer
    assert category == 'vpc_id'
    assert sum(counts.values()) == 1
    assert diagnostics['question_driver_resolved_fields'] == ['vpc_id']


def test_unresolved_missing_fact_still_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'missing_fields': ['vpc_id']})
    with pytest.raises(RuntimeError, match='unavailable case facts: vpc_id'):
        driver.answer_question(tmp_path, {'question': 'VpcId?'}, {'goal': '仅规划'}, {}, {},
            fact_resolver=lambda _fields: {'vpc_id': '', 'goal': 'fake replacement'})


def test_existing_facts_do_not_trigger_resolver(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {'fact_keys': ['goal']})
    driver.answer_question(tmp_path, {'question': '用途?'}, {'goal': '仅规划'}, {}, {},
        fact_resolver=lambda _: pytest.fail('must not fetch unrequested network facts'))


def test_fixture_exclusion_uses_accepted_receipts_and_never_reads_unrelated_stacks(monkeypatch):
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun import ros_client
    calls = []
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    monkeypatch.setattr(driver, '_fixture_creation_receipts', lambda: [
        {'stackId': 'accepted-1', 'regionId': 'cn-hangzhou', 'stackName': 'arbitrary-model-name'},
        {'stackId': 'accepted-2', 'regionId': 'cn-hangzhou', 'stackName': 'another-model-name'},
    ])
    class Client:
        def list_stacks(self, _request):
            pytest.fail('do not scan unrelated shared account stacks')
        def list_stack_resources(self, request):
            calls.append(request)
            assert request.stack_id in {'accepted-1', 'accepted-2'}
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {'Resources': [
                {'ResourceType': 'ALIYUN::ECS::VPC', 'Status': 'CREATE_COMPLETE',
                 'PhysicalResourceId': 'vpc-' + request.stack_id},
                {'ResourceType': 'ALIYUN::ECS::VPC', 'Status': 'DELETE_COMPLETE', 'PhysicalResourceId': 'vpc-gone'},
            ]}))
    monkeypatch.setattr(cloud_credentials, 'CloudCredentials', lambda: SimpleNamespace(
        get_provider=lambda _: SimpleNamespace(region_id='cn-hangzhou')))
    monkeypatch.setattr(ros_client.RosClientFactory, 'create', lambda *_: Client())
    assert driver.temporary_e2e_vpc_ids() == {'vpc-accepted-1', 'vpc-accepted-2'}
    assert len(calls) == 2


def test_empty_fixture_receipts_need_no_global_cloud_inventory(monkeypatch):
    from iac_code.tools.cloud.aliyun import ros_client
    monkeypatch.setattr(driver, '_fixture_creation_receipts', lambda: [])
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    monkeypatch.setattr(ros_client.RosClientFactory, 'create', lambda *_: pytest.fail('no accepted Stack to query'))
    assert driver.temporary_e2e_vpc_ids() == set()


def _write_fixture_receipt(config, stack_id, *, source_attempt='attempt'):
    import yaml
    pipeline = config / 'projects/project/session/pipeline'
    pipeline.mkdir(parents=True)
    (pipeline / 'meta.yaml').write_text(yaml.safe_dump({
        'attempts': {'items': {'attempt': {'step_id': 'deploying'}}},
    }), encoding='utf-8')
    (pipeline / 'cleanup.yaml').write_text(yaml.safe_dump({'observed_resources': [{
        'provider': 'ros', 'resource_type': 'stack', 'observed_action': 'CreateStack',
        'resource_id': stack_id, 'resource_name': 'model-chosen-name', 'region_id': 'cn-hangzhou',
        'source_step_id': 'deploying', 'source_attempt_id': source_attempt,
        'metadata': {'tool_name': 'ros_deploy', 'tool_use_id': 'create-call'},
    }]}), encoding='utf-8')


def test_fixture_receipt_scope_includes_live_siblings_but_not_closed_or_external_cases(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    _write_fixture_receipt(root / 'live/config', 'accepted-live')
    _write_fixture_receipt(root / 'nested/scenario-token/config', 'accepted-nested')
    _write_fixture_receipt(root / 'live/retry-1/config', 'accepted-retry')
    _write_fixture_receipt(root / 'nested/retry-1/scenario-token/config', 'accepted-retry-nested')
    _write_fixture_receipt(root / 'closed/config', 'accepted-closed')
    (root / 'closed/ci-result.json').write_text(json.dumps({'cleanupStatus': 'completed'}), encoding='utf-8')
    _write_fixture_receipt(tmp_path / 'external/config', 'accepted-external')
    monkeypatch.setenv('IAC_CODE_E2E_CASES_DIR', str(root))
    assert {r['stackId'] for r in driver._fixture_creation_receipts()} == {
        'accepted-live', 'accepted-nested', 'accepted-retry', 'accepted-retry-nested',
    }


def test_fixture_receipt_scope_rejects_unproven_attempt_instead_of_querying_claimed_stack(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    _write_fixture_receipt(root / 'bad/config', 'claimed-stack', source_attempt='unrelated-attempt')
    monkeypatch.setenv('IAC_CODE_E2E_CASES_DIR', str(root))
    with pytest.raises(ValueError, match='does not belong'):
        driver._fixture_creation_receipts()


def test_fixture_exclusion_keeps_valid_receipts_during_partial_sibling_summary_write(tmp_path, monkeypatch):
    root = tmp_path / 'runs'
    _write_fixture_receipt(root / 'finishing/config', 'accepted-finishing')
    summary = root / 'finishing/ci-result.json'
    summary.write_text('{"cleanupStatus":', encoding='utf-8')
    monkeypatch.setenv('IAC_CODE_E2E_CASES_DIR', str(root))
    assert [r['stackId'] for r in driver._fixture_creation_receipts()] == ['accepted-finishing']
    summary.write_text(json.dumps({'cleanupStatus': 'completed'}), encoding='utf-8')
    assert driver._fixture_creation_receipts() == []


@pytest.mark.parametrize('code,family,terms', [
    ('Forbidden.ResourceGroup.private-secret', 'Forbidden', ['Resource', 'ResourceGroup']),
    ('private-secret', 'unknown', []),
])
def test_fixture_inventory_error_projects_fixed_code_family_only(tmp_path, monkeypatch, code, family, terms):
    class ClientFailureError(RuntimeError):
        pass
    error = ClientFailureError('private provider payload')
    error.code = code
    monkeypatch.setenv('IAC_CODE_CONFIG_DIR', str(tmp_path))
    def fail():
        raise error
    monkeypatch.setattr(driver, '_temporary_e2e_vpc_ids_once', fail)
    with pytest.raises(ClientFailureError):
        driver.temporary_e2e_vpc_ids()
    data = json.loads((tmp_path / driver.NETWORK_DIAGNOSTIC_FILENAME).read_text(encoding='utf-8'))
    assert data['network_fixture_sdk_code_family'] == family
    assert data['network_fixture_sdk_code_terms'] == terms
    assert 'private' not in json.dumps(data)


def test_network_fixture_code_skips_temporary_vpc_even_when_listed_first(monkeypatch, capsys):
    from scripts.repl.e2e import run_pipeline_scenarios as repl
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    monkeypatch.setattr(driver, 'temporary_e2e_vpc_ids', lambda: {'vpc-temporary'})
    def api(_product, action, params):
        if action == 'DescribeVpcs':
            return {'Vpcs': {'Vpc': [{'VpcId': v, 'CidrBlock': '10.250.0.0/16', 'Status': 'Available',
                                     'CreationTime': '2020-01-01T00:00:00Z'}
                                     for v in ('vpc-temporary', 'vpc-stable')]}}
        if action == 'DescribeZones':
            return {'Zones': {'Zone': [{'ZoneId': 'cn-hangzhou-i'}]}}
        assert params['VpcId'] == 'vpc-stable'
        return {'VSwitches': {'VSwitch': []}}
    monkeypatch.setattr(repl, '_call_aliyun_api', api)
    monkeypatch.setattr(sys, 'argv', ['fixture', '10.250.1.0/24'])
    exec(driver._NETWORK_FACTS_CODE, {})
    assert json.loads(capsys.readouterr().out)['vpc_id'] == 'vpc-stable'


def test_network_fixture_rechecks_sibling_ownership_after_network_queries(monkeypatch, capsys):
    from scripts.repl.e2e import run_pipeline_scenarios as repl
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    published = set()
    monkeypatch.setattr(driver, 'temporary_e2e_vpc_ids', lambda: published.copy())

    def api(_product, action, params):
        if action == 'DescribeVpcs':
            return {'Vpcs': {'Vpc': [{'VpcId': v, 'CidrBlock': '10.250.0.0/16', 'Status': 'Available',
                                     'CreationTime': '2020-01-01T00:00:00Z'}
                                     for v in ('vpc-temporary', 'vpc-stable')]}}
        if action == 'DescribeZones':
            return {'Zones': {'Zone': [{'ZoneId': 'cn-hangzhou-i'}]}}
        assert action == 'DescribeVSwitches'
        if params['VpcId'] == 'vpc-temporary':
            # ROS publishes the sibling's physical VPC after our initial scan.
            published.add('vpc-temporary')
        return {'VSwitches': {'VSwitch': []}}

    monkeypatch.setattr(repl, '_call_aliyun_api', api)
    monkeypatch.setattr(sys, 'argv', ['fixture', '10.250.1.0/24'])
    exec(driver._NETWORK_FACTS_CODE, {})
    assert json.loads(capsys.readouterr().out)['vpc_id'] == 'vpc-stable'


@pytest.mark.parametrize('status,created,eligible', [
    ('Available', '2020-01-01T00:00:00Z', True),
    ('Pending', '2020-01-01T00:00:00Z', False),
    ('Available', '2026-10-06T00:00:00Z', False),
    ('Available', '2026-10-06T00:00:01Z', False),
    ('Available', None, False), ('Available', 'private-invalid', False),
    ('Available', '2020-01-01T00:00:00', False),
])
def test_network_fixture_requires_available_resource_predating_invocation(status, created, eligible):
    cutoff = datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp()
    assert driver.network_fixture_vpc_is_eligible({'Status': status, 'CreationTime': created}, cutoff) is eligible


def test_network_fixture_never_selects_pending_or_new_unpublished_vpc(monkeypatch, capsys):
    from scripts.repl.e2e import run_pipeline_scenarios as repl
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    monkeypatch.setattr(driver, 'temporary_e2e_vpc_ids', lambda: set())
    cutoff = datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp()
    monkeypatch.setenv('IAC_CODE_E2E_NETWORK_FIXTURE_BEFORE', str(cutoff))

    def api(_product, action, params):
        if action == 'DescribeVpcs':
            return {'Vpcs': {'Vpc': [
                {'VpcId': 'vpc-pending', 'Status': 'Pending', 'CreationTime': '2025-01-01T00:00:00Z',
                 'CidrBlock': '10.250.0.0/16'},
                {'VpcId': 'vpc-new-unpublished', 'Status': 'Available',
                 'CreationTime': '2026-10-06T00:00:01Z', 'CidrBlock': '10.250.0.0/16'},
                {'VpcId': 'vpc-stable', 'Status': 'Available', 'CreationTime': '2020-01-01T00:00:00Z',
                 'CidrBlock': '10.250.0.0/16'},
            ]}}
        if action == 'DescribeZones':
            return {'Zones': {'Zone': [{'ZoneId': 'cn-hangzhou-i'}]}}
        return {'VSwitches': {'VSwitch': []}}

    monkeypatch.setattr(repl, '_call_aliyun_api', api)
    monkeypatch.setattr(sys, 'argv', ['fixture', '10.250.1.0/24'])
    exec(driver._NETWORK_FACTS_CODE, {})
    assert json.loads(capsys.readouterr().out)['vpc_id'] == 'vpc-stable'


def test_missing_detail_records_only_fixed_question_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['purpose', 'private-secret'], 'option_id': 'private-option', 'missing_fields': ['other']})
    diagnostics = {}
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': 'AWS 预算和并发规模? private-secret',
            'options': [{'id': 'private-option', 'label': 'private-label'}]},
            {'goal': '只询价', 'purpose': 'private-purpose'}, {}, diagnostics)
    assert diagnostics['question_driver_question_subjects'] == ['budget', 'cloud_vendor', 'scale']
    assert diagnostics['question_driver_available_fact_keys'] == ['goal', 'purpose']
    assert diagnostics['question_driver_selected_fact_keys'] == ['purpose']
    assert diagnostics['question_driver_option_count'] == 1
    assert diagnostics['question_driver_option_selected'] is True
    assert diagnostics['question_driver_free_text_allowed'] is True
    assert 'private-' not in json.dumps(diagnostics)


def test_literal_qualitative_scale_budget_and_cloud_are_available_without_invention():
    goal = '小团队 Node.js 电商 API；只规划阿里云杭州低成本网络；本轮不部署'
    facts = driver.case_facts(goal)
    assert facts['scale'] == '小团队 Node.js 电商 API'
    assert facts['budget'] == '只规划阿里云杭州低成本网络'
    assert facts['cloud_vendor'] == '只规划阿里云杭州低成本网络'
    assert 'QPS' not in json.dumps(facts) and '人民币' not in json.dumps(facts)
    aws = driver.case_facts('为 AWS 创建 Amazon VPC；不使用阿里云，也不生成 ROS 模板')
    assert 'AWS' in aws['cloud_vendor']
    assert aws['constraints'] == '不使用阿里云，也不生成 ROS 模板'
    unspecified = driver.case_facts('创建 VSwitch')
    assert not {'scale', 'budget', 'cloud_vendor', 'region'}.intersection(unspecified)


def test_plain_network_planning_is_indexed_as_literal_resource_scope():
    goal = '只规划阿里云杭州低成本网络，本轮不部署'
    facts = driver.case_facts(goal)
    assert facts['resource_scope'] == goal


@pytest.mark.parametrize('missing', ['scale', 'other'])
def test_empty_helper_selection_can_honestly_restate_unspecified_preferences(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': [], 'missing_fields': [missing], 'missing_detail': 'business_preference', 'option_id': ''})
    facts = driver.case_facts('仅在阿里云杭州的已有 VPC 创建 VSwitch，本轮不部署')
    diagnostics = {}
    answer, _ = driver.answer_question(tmp_path, {'question': '应用用途和业务规模?', 'allowFreeText': True},
                                       facts, {}, diagnostics)
    assert facts['goal'] in answer and '尚未指定' in answer and '不得虚构' in answer
    assert 'QPS=' not in answer and '当前问题选择' not in answer
    assert diagnostics['question_driver_facts_fallback_count'] == 1


@pytest.mark.parametrize('question,missing', [('必填规模是多少?', 'scale'), ('请提供 VpcId', 'other'),
                                            ('NoEcho Password?', 'other')])
def test_empty_helper_selection_cannot_invent_required_or_secret_facts(tmp_path, monkeypatch, question, missing):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': [], 'missing_fields': [missing], 'missing_detail': 'business_preference'})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': question}, driver.case_facts('仅规划低成本网络'), {}, {})


def test_missing_future_region_is_reviewed_against_current_grounded_cloud_option(tmp_path, monkeypatch):
    calls = []
    def choose(_config, pending, facts):
        calls.append(pending)
        if len(calls) == 1:
            return {'fact_keys': ['goal'], 'option_id': 'aws', 'missing_fields': ['region']}
        assert pending['_fact_selection_review']['missing_fields'] == ['region']
        assert facts['goal'] == 'AWS VPC；不使用阿里云，也不生成 ROS 模板'
        return {'fact_keys': ['cloud_vendor'], 'option_id': 'aws', 'missing_fields': []}
    monkeypatch.setattr(driver, '_select_facts', choose)
    diagnostics, counts = {}, {}
    answer, _ = driver.answer_question(tmp_path, {'question': '请选择云厂商',
        'options': [{'id': 'aws', 'label': 'Amazon AWS'}, {'id': 'aliyun', 'label': '阿里云'}]},
        driver.case_facts('AWS VPC；不使用阿里云，也不生成 ROS 模板'), counts, diagnostics)
    assert '当前问题选择：Amazon AWS' in answer
    assert '不生成 ROS 模板' in answer and 'us-east' not in answer
    assert len(calls) == 2 and sum(counts.values()) == 1
    assert diagnostics['question_driver_review_count'] == 1
    assert diagnostics['question_driver_answer_count'] == 1


def test_scale_review_can_use_existing_small_team_fact_without_a_numeric_capacity(tmp_path, monkeypatch):
    calls = []
    def choose(_config, pending, facts):
        calls.append(pending)
        if len(calls) == 1:
            return {'fact_keys': list(facts), 'missing_fields': ['other']}
        assert '小团队' in facts['scale']
        return {'fact_keys': ['purpose', 'scale', 'budget'], 'missing_fields': []}
    monkeypatch.setattr(driver, '_select_facts', choose)
    goal = '小团队 Node.js 电商 API，只规划阿里云杭州低成本网络，本轮不部署、不创建资源'
    answer, _ = driver.answer_question(tmp_path, {'question': '产品用途和预期规模?'},
                                       driver.case_facts(goal), {}, {})
    assert answer == goal
    assert len(calls) == 2


@pytest.mark.parametrize('review', [None, {}, {'fact_keys': ['invented'], 'missing_fields': []},
    {'fact_keys': ['goal'], 'missing_fields': ['region']}])
def test_missing_review_cannot_fall_back_or_keep_retrying_when_fact_is_unavailable(tmp_path, monkeypatch, review):
    calls = []
    def choose(*_args):
        calls.append(True)
        return {'fact_keys': ['goal'], 'missing_fields': ['region']} if len(calls) == 1 else review
    monkeypatch.setattr(driver, '_select_facts', choose)
    diagnostics = {}
    with pytest.raises(RuntimeError, match='unavailable case facts: region'):
        driver.answer_question(tmp_path, {'question': '必须指定部署地域'}, {'goal': '创建网络'}, {}, diagnostics)
    assert len(calls) == 2 and diagnostics['question_driver_review_count'] == 1
    assert 'question_driver_answer_count' not in diagnostics


def test_real_option_can_be_answered_with_an_honest_undecided_preference(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cloud_vendor'], 'option_id': 'aws', 'missing_fields': ['region']})
    diagnostics = {}
    answer, _ = driver.answer_question(tmp_path, {'question': '云厂商?',
        'options': [{'id': 'aws', 'label': 'Amazon AWS'}]},
        driver.case_facts('只用 AWS，不使用阿里云，也不生成 ROS 模板'), {}, diagnostics)
    assert '当前问题选择：Amazon AWS' in answer and '尚未指定的补充细节：地域' in answer
    assert '不得虚构' in answer and '不使用阿里云' in answer
    assert diagnostics['question_driver_unspecified_preferences'] == ['region']
    assert 'us-east' not in answer and 'cn-hangzhou' not in answer


def test_actual_security_option_can_answer_without_inventing_additional_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cidr'], 'option_id': 'private', 'missing_fields': ['constraints'], 'missing_detail': 'unknown'})
    goal = '请在阿里云杭州为测试应用设计网络基础设施'
    facts = driver.case_facts(goal, {'cidr': '10.250.1.0/24'})
    diagnostics = {}
    answer, _ = driver.answer_question(tmp_path, {'question': '安全组必需的访问来源范围?',
        'options': [{'id': 'private', 'label': '仅指定网段'}, {'id': 'all', 'label': '全部来源'}]},
        facts, {}, diagnostics)
    assert goal in answer and '10.250.1.0/24' in answer and '当前问题选择：仅指定网段' in answer
    assert '尚未指定的补充细节：其他限制' in answer and '不得虚构' in answer
    assert '0.0.0.0' not in answer and '全部来源' not in answer
    assert diagnostics['question_driver_unspecified_preferences'] == ['constraints']


@pytest.mark.parametrize('detail,option', [('unknown', ''), ('resource_id', 'private'),
                                        ('resource_name', 'private')])
def test_missing_constraints_cannot_supply_required_values_or_bypass_without_option(tmp_path, monkeypatch, detail,
                                                                                 option):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'option_id': option, 'missing_fields': ['constraints'], 'missing_detail': detail})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': '必须提供安全组配置',
            'options': [{'id': 'private', 'label': '指定范围'}]}, {'goal': '规划网络'}, {}, {})


@pytest.mark.parametrize('question', ['必填 VpcId?', 'NoEcho Password?'])
def test_missing_identity_mislabeled_constraints_still_fails(tmp_path, monkeypatch, question):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'option_id': 'known', 'missing_fields': ['constraints'], 'missing_detail': 'unknown'})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': question,
            'options': [{'id': 'known', 'label': '使用当前配置'}]}, {'goal': '规划网络'}, {}, {})


@pytest.mark.parametrize('missing', ['vpc_id', 'zone_id', 'cidr', 'stack_name', 'other'])
def test_option_never_bypasses_a_missing_required_resource_fact(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'option_id': 'existing', 'missing_fields': [missing]})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': '必填参数?',
            'options': [{'id': 'existing', 'label': '使用已有资源'}]}, {'goal': '只规划'}, {}, {})


def test_fixture_owned_stack_disappearance_is_safe_but_permission_failure_is_not(monkeypatch):
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun import ros_client
    class CloudFailureError(RuntimeError):
        code = 'EntityNotExist.Stack'
    class Client:
        def list_stack_resources(self, _request):
            raise CloudFailureError('private SDK payload')
    monkeypatch.setattr(driver, '_fixture_creation_receipts', lambda: [
        {'stackId': 'accepted-stack', 'regionId': 'cn-hangzhou'}])
    monkeypatch.setattr(cloud_credentials.CloudCredentials, 'get_provider', lambda *_: SimpleNamespace(
        region_id='cn-hangzhou'))
    monkeypatch.setattr(ros_client.RosClientFactory, 'create', lambda *_: Client())
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    assert driver.temporary_e2e_vpc_ids() == set()
    CloudFailureError.code = 'Forbidden.RAM'
    with pytest.raises(CloudFailureError):
        driver.temporary_e2e_vpc_ids()


def test_fixture_rescan_is_bounded_and_does_not_retry_authentication_errors(monkeypatch):
    calls = []
    class FailureError(RuntimeError):
        code = 'InvalidAccessKeyId'
    def scan():
        calls.append(True)
        raise FailureError('private')
    monkeypatch.setattr(driver, '_temporary_e2e_vpc_ids_once', scan)
    with pytest.raises(FailureError):
        driver.temporary_e2e_vpc_ids()
    assert len(calls) == 1
    FailureError.code = 'StackNotFound'
    calls.clear()
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    monkeypatch.setattr(driver, '_write_network_diagnostic', lambda *_: None)
    with pytest.raises(FailureError):
        driver.temporary_e2e_vpc_ids()
    assert len(calls) == 3


def test_network_failure_diagnostic_keeps_only_known_codes_not_private_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(driver.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(
        returncode=1, stdout='private cloud data', stderr='EntityNotExist.Stack private credential'))
    with pytest.raises(RuntimeError) as error:
        driver.network_facts('python', {'IAC_CODE_CONFIG_DIR': str(tmp_path)}, tmp_path, '10.0.1.0/24')
    value = json.loads((tmp_path / driver.NETWORK_DIAGNOSTIC_FILENAME).read_text(encoding='utf-8'))
    assert value == {'network_fixture_failure_category': 'stack_disappeared', 'network_fixture_exit_code': 1,
                     'network_fixture_known_codes': ['EntityNotExist.Stack'], 'network_fixture_error_types': []}
    assert 'private' not in json.dumps(value) and 'credential' not in str(error.value)


@pytest.mark.parametrize('detail', ['subnet_cidr', 'private raw error'])
def test_missing_detail_diagnostic_is_bounded_and_does_not_bypass_unknown_fact(tmp_path, monkeypatch, detail):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cidr'], 'option_id': 'known', 'missing_fields': ['other'], 'missing_detail': detail,
    })
    diagnostics = {}
    with pytest.raises(RuntimeError, match='unavailable case facts: other'):
        driver.answer_question(tmp_path, {'question': 'CIDR?', 'options': [{'id': 'known', 'label': '网段'}]},
                               {'goal': '只询价，不部署', 'cidr': '192.168.24.0/24'}, {}, diagnostics)
    assert diagnostics['question_driver_missing_fields'] == ['other']
    if detail == 'subnet_cidr':
        assert diagnostics['question_driver_missing_detail'] == detail
    else:
        assert 'question_driver_missing_detail' not in diagnostics
def test_parallel_fixture_queries_reserve_distinct_unoccupied_subnets(tmp_path):
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / 'reservations.json'
    network = ipaddress.ip_network('10.1.0.0/16')
    occupied = [ipaddress.ip_network('10.1.0.0/24')]
    desired = ipaddress.ip_network('10.250.1.0/24')
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: driver.reserve_network_subnet(
            'private-vpc', network, occupied, desired, str(path)), range(12)))
    assert len(set(results)) == 12
    assert all(result.subnet_of(network) and not result.overlaps(occupied[0]) for result in results)
    assert 'private-vpc' not in path.read_text(encoding='utf-8')
    assert not path.with_name(path.name + '.lock').exists()


@pytest.mark.parametrize('failures', [1, 3])
def test_fixture_reservation_retries_windows_pending_delete_access_error(tmp_path, monkeypatch, failures):
    import ipaddress
    from pathlib import Path

    path = tmp_path / 'reservations.json'
    lock = path.with_name(path.name + '.lock')
    mkdir = Path.mkdir
    attempts = 0

    def pending_delete_mkdir(self, *args, **kwargs):
        nonlocal attempts
        if self == lock:
            attempts += 1
            if attempts <= failures:
                error = PermissionError('Windows lock directory is pending deletion')
                error.winerror = 5
                raise error
        return mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'mkdir', pending_delete_mkdir)
    monkeypatch.setattr(driver, 'time', SimpleNamespace(monotonic=driver.time.monotonic, sleep=lambda _: None))
    network = ipaddress.ip_network('10.1.0.0/16')
    desired = ipaddress.ip_network('10.1.2.0/24')
    assert driver.reserve_network_subnet('vpc', network, [], desired, str(path)) == desired
    assert attempts == failures + 1
    assert not lock.exists()


@pytest.mark.parametrize('winerror', [None, 5, 32])
def test_fixture_reservation_access_errors_never_bypass_lock(tmp_path, monkeypatch, winerror):
    import ipaddress
    from pathlib import Path

    path = tmp_path / 'reservations.json'
    lock = path.with_name(path.name + '.lock')
    mkdir = Path.mkdir
    error = PermissionError('lock access denied')
    if winerror is not None:
        error.winerror = winerror

    def denied_mkdir(self, *args, **kwargs):
        if self == lock:
            raise error
        return mkdir(self, *args, **kwargs)

    clock = iter([0, 16])
    monkeypatch.setattr(driver, 'time', SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.setattr(Path, 'mkdir', denied_mkdir)
    network = ipaddress.ip_network('10.1.0.0/16')
    expected = TimeoutError if winerror == 5 else PermissionError
    with pytest.raises(expected) as caught:
        driver.reserve_network_subnet('vpc', network, [], network, str(path))
    if winerror == 5:
        assert caught.value.__cause__ is error
    assert not path.exists()
    assert not lock.exists()


def test_fixture_reservation_does_not_override_unavailable_subnet_or_corrupt_registry(tmp_path):
    import ipaddress

    network = ipaddress.ip_network('10.1.0.0/24')
    path = tmp_path / 'reservations.json'
    assert driver.reserve_network_subnet('vpc', network, [network], network, str(path)) is None
    path.write_text('invalid JSON', encoding='utf-8')
    with pytest.raises(ValueError):
        driver.reserve_network_subnet('vpc', network, [], network, str(path))
    assert not path.with_name(path.name + '.lock').exists()


def test_current_provider_answer_does_not_require_future_network_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cloud_vendor', 'constraints'], 'option_id': '', 'missing_fields': ['region', 'cidr']})
    diagnostics = {}
    goal = '请为 AWS 账号创建一个 Amazon VPC，不使用阿里云，也不生成 ROS 模板。'
    answer, _ = driver.answer_question(tmp_path, {'question': '当前服务面向阿里云，是否坚持使用 AWS?',
        'options': [{'id': 'aws', 'label': '坚持 AWS'}, {'id': 'aliyun', 'label': '改用阿里云'}]},
        driver.case_facts(goal, {'cloud_vendor': 'AWS'}), {}, diagnostics,
        fact_resolver=lambda *_: pytest.fail('unrelated planning facts must not trigger cloud queries'))
    assert goal in answer and '尚未指定的补充细节：网段、地域' in answer
    assert 'us-east' not in answer and 'cn-hangzhou' not in answer and '/24' not in answer
    assert diagnostics['question_driver_review_count'] == 1
    assert diagnostics['question_driver_unspecified_preferences'] == ['cidr', 'region']


@pytest.mark.parametrize('question,missing', [
    ('AWS VPC 的 CIDR 网段是多少?', 'cidr'),
    ('AWS VPC 的网段前缀是多少?', 'cidr_prefix'),
    ('AWS 部署地域是什么?', 'region'),
    ('AWS 必须指定网络参数信息', 'cidr'),
    ('请提供必填参数', 'cidr'),
    ('AWS 的 VpcId 是什么?', 'vpc_id'),
    ('AWS 的其他必填信息是什么?', 'other'),
])
def test_scoped_provider_fact_never_bypasses_current_missing_detail(tmp_path, monkeypatch, question, missing):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cloud_vendor'], 'option_id': '', 'missing_fields': [missing]})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': question},
                               driver.case_facts('仅使用 AWS', {'cloud_vendor': 'AWS'}), {}, {})


def test_unrelated_preferences_do_not_authorize_an_option_without_free_text(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cloud_vendor'], 'option_id': '', 'missing_fields': ['region', 'cidr']})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': '选择云厂商 AWS?', 'allowFreeText': False},
                               driver.case_facts('仅使用 AWS', {'cloud_vendor': 'AWS'}), {}, {})


@pytest.mark.parametrize('detail', [None, 'unknown', 'business_preference'])
def test_unmapped_helper_detail_restates_actual_facts_without_inventing_an_answer(tmp_path, monkeypatch, detail):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['purpose'], 'missing_fields': ['other'], 'missing_detail': detail,
        'option_id': 'create', 'answer': 'invented capacity'})
    facts = driver.case_facts('只规划杭州低成本 VPC，本轮不部署', {'cidr': '10.0.1.0/24'})
    diagnostics, counts = {}, {}
    answer, _ = driver.answer_question(tmp_path, {'question': '用途和规模?',
        'options': [{'id': 'create', 'label': '部署'}]}, facts, counts, diagnostics)
    assert all(value in answer for value in facts.values())
    assert '其他补充信息' in answer and '尚未指定' in answer and '不得虚构' in answer
    assert 'invented' not in answer and '当前问题选择' not in answer
    assert diagnostics['question_driver_unresolved_fields'] == ['other']
    assert diagnostics['question_driver_unknown_detail_restated_count'] == 1 and sum(counts.values()) == 1


@pytest.mark.parametrize('question,detail', [
    ('请提供 VpcId', 'unknown'), ('请提供 ZoneId', 'unknown'), ('CIDR 网段?', 'unknown'),
    ('NoEcho Password?', 'business_preference'), ('必填其他信息?', 'unknown'),
    ('已有资源的标识?', 'resource_id'), ('子网?', 'subnet_cidr'),
])
def test_unknown_helper_verdict_never_invents_required_or_secret_fields(tmp_path, monkeypatch, question, detail):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['other'], 'missing_detail': detail})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': question}, {'goal': '只规划网络'}, {}, {})


def test_unknown_detail_restatement_remains_bounded_until_product_acknowledges(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['other'], 'missing_detail': 'unknown'})
    counts, diagnostics = {}, {}
    for _ in range(driver.MAX_REPEATS):
        answer, _ = driver.answer_question(tmp_path, {'question': '其他偏好?'},
                                           {'goal': '只规划，不部署'}, counts, diagnostics)
        assert '尚未指定' in answer
    with pytest.raises(RuntimeError, match='budget exhausted'):
        driver.answer_question(tmp_path, {'question': '其他偏好?'},
                               {'goal': '只规划，不部署'}, counts, diagnostics)


@pytest.mark.parametrize('detail,state', [(None, 'null'), ('unknown', 'valid'),
                                         ('private-response', 'invalid'), ({'private': 'secret'}, 'invalid')])
def test_blocked_question_exports_decision_category_without_helper_payload(tmp_path, monkeypatch, detail, state):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['other'], 'missing_detail': detail})
    diagnostics = {}
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': '必填 NoEcho Password?'},
                               {'goal': '只规划'}, {}, diagnostics)
    assert diagnostics['question_driver_missing_detail_state'] == state
    assert diagnostics['question_driver_required_word_present'] is True
    assert 'private' not in json.dumps(diagnostics)


def test_other_missing_detail_resolves_question_identity_and_reviews_real_facts(tmp_path, monkeypatch):
    calls, queries = [], []
    def choose(_config, _pending, facts):
        calls.append(dict(facts))
        return ({'fact_keys': ['cidr', 'vpc_id'], 'missing_fields': [], 'missing_detail': 'resource_id'}
                if 'vpc_id' in facts else
                {'fact_keys': ['cidr'], 'missing_fields': ['other'], 'missing_detail': 'business_preference'})
    monkeypatch.setattr(driver, '_select_facts', choose)
    def resolve(fields):
        queries.append(fields)
        return {'vpc_id': 'vpc-real-fixture', 'zone_id': 'unrequested', 'goal': 'untrusted replacement'}
    diagnostics, counts = {}, {}
    original = {'goal': '只规划，不部署', 'cidr': '10.0.1.0/24'}
    answer, category = driver.answer_question(tmp_path, {'question': '请选择已有 VPC，并提供网段'},
        original, counts, diagnostics, fact_resolver=resolve)
    assert queries == [('vpc_id',)] and len(calls) == 3
    assert 'vpc-real-fixture' in answer and '10.0.1.0/24' in answer and '不部署' in answer
    assert 'untrusted' not in answer and 'unrequested' not in answer
    assert 'vpc_id' not in original and sum(counts.values()) == 1
    assert category == 'vpc_id' and diagnostics['question_driver_identity_review_count'] == 1


@pytest.mark.parametrize('resolved', [{}, {'vpc_id': ''}])
def test_other_identity_resolution_cannot_bypass_withheld_or_missing_identity(tmp_path, monkeypatch, resolved):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cidr'], 'missing_fields': ['other'], 'missing_detail': 'unknown'})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': '已有 VPC 的 VpcId 和网段?'},
            {'goal': '本轮不部署', 'cidr': '10.0.1.0/24'}, {}, {}, fact_resolver=lambda _: resolved)


def test_other_identity_review_does_not_satisfy_an_unknown_required_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cidr'], 'missing_fields': ['other'], 'missing_detail': 'resource_id'})
    with pytest.raises(RuntimeError, match='unavailable case facts'):
        driver.answer_question(tmp_path, {'question': '必填 VpcId 和其他资源 ID?'},
            {'goal': '本轮不部署', 'cidr': '10.0.1.0/24'}, {}, {},
            fact_resolver=lambda _: {'vpc_id': 'vpc-real'})


def test_choice_only_question_reviews_an_invalid_helper_option_once(tmp_path, monkeypatch):
    selections = iter([
        {'fact_keys': ['goal'], 'option_id': ''},
        {'fact_keys': ['goal'], 'option_id': 'existing-vpc', 'missing_fields': []},
    ])
    pending_calls = []
    def select(_config, pending, _facts):
        pending_calls.append(pending)
        return next(selections)
    monkeypatch.setattr(driver, '_select_facts', select)
    diagnostics = {}
    answer, category = driver.answer_question(tmp_path, {'question': '网络规划方式？', 'allowFreeText': False,
        'options': [{'id': 'existing-vpc', 'label': '复用已有 VPC'}]},
        {'goal': '复用已有 VPC，规划安全组，本轮不部署'}, {}, diagnostics)
    assert (answer, category) == ('existing-vpc', 'option')
    assert len(pending_calls) == 2
    assert pending_calls[1]['_fact_selection_review']['issue'] == 'invalid_option_selection'
    assert diagnostics['question_driver_answer_count'] == 1


def test_deferred_resource_id_is_honestly_withheld_until_user_required_implementation_question(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal', 'purpose'], 'missing_fields': ['vpc_id'], 'missing_detail': 'resource_id'})
    facts = {'goal': '先规划方案，实现阶段再询问 VpcId，不能默认选择', 'purpose': '测试应用'}
    diagnostics = {}
    answer, category = driver.answer_question(tmp_path, {
        'question': '产品用途与架构?', 'allowFreeText': True, '_deferred_fact_fields': ['vpc_id']},
        facts, {}, diagnostics, fact_resolver=lambda _: pytest.fail('deferred ID must not be looked up early'))
    assert facts['goal'] in answer and '测试应用' in answer
    assert category == 'goal' and 'vpc-' not in answer
    assert diagnostics['question_driver_deferred_fields'] == ['vpc_id']


def test_deferred_id_never_satisfies_missing_free_text_or_other_required_field(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['vpc_id', 'zone_id'], 'missing_detail': 'resource_id'})
    with pytest.raises(RuntimeError, match='zone_id'):
        driver.answer_question(tmp_path, {'question': 'VpcId和ZoneId?', 'allowFreeText': True,
                              '_deferred_fact_fields': ['vpc_id']}, {'goal': '只推迟VpcId'}, {}, {})


@pytest.mark.parametrize('missing', ['scale', 'other'])
def test_planning_preference_can_be_unspecified_while_existing_vpc_is_explicitly_deferred(
    tmp_path, monkeypatch, missing,
):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['purpose', 'region'], 'option_id': '',
        'missing_fields': [missing], 'missing_detail': 'unknown'})
    facts = {'goal': '复用已有 VPC 创建 VSwitch，先规划，实现阶段再询问 VpcId，不能自行查询或默认选择',
             'purpose': '测试应用', 'region': '阿里云杭州'}
    diagnostics = {}
    def resolve(fields):
        assert not {'vpc_id', 'zone_id'}.intersection(fields), 'deferred ID must not be queried'
        return {}
    answer, category = driver.answer_question(tmp_path, {
        'question': '请说明云厂商、应用规模以及复用已有 VPC 的规划目标？', 'allowFreeText': True,
        '_deferred_fact_fields': ['vpc_id'], 'options': [{'id': 'other', 'label': '其他目标'}]},
        facts, {}, diagnostics, fact_resolver=resolve)
    assert category == 'goal' and facts['goal'] in answer
    assert '尚未指定的补充细节' in answer and '不得虚构' in answer
    assert '当前尚未提供VpcId' in answer and 'vpc-' not in answer
    assert diagnostics['question_driver_deferred_fields'] == ['vpc_id']
    assert diagnostics['question_driver_option_selected'] is False


@pytest.mark.parametrize('pending', [
    {}, {'_deferred_fact_fields': ['zone_id']}, {'_deferred_fact_fields': 'vpc_id'},
])
def test_missing_preference_cannot_hide_an_undeclared_existing_vpc_identity(tmp_path, monkeypatch, pending):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['scale'], 'missing_detail': 'unknown'})
    with pytest.raises(RuntimeError, match='unavailable case facts: scale'):
        driver.answer_question(tmp_path, {'question': '应用规模和复用已有 VPC 的规划目标？', **pending},
                               {'goal': '复用已有 VPC 创建 VSwitch'}, {}, {})


@pytest.mark.parametrize(('question', 'free_text'), [
    ('复用已有 VPC，但应用规模是必填参数', True),
    ('复用已有 VPC，应用规模和 ZoneId 是多少？', True),
    ('复用已有 VPC，应用规模和数据库密码是什么？', True),
    ('复用已有 VPC 的应用规模？', False),
])
def test_deferred_vpc_does_not_bypass_required_preference_secret_or_other_identity(
    tmp_path, monkeypatch, question, free_text,
):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['scale'], 'missing_detail': 'unknown'})
    with pytest.raises(RuntimeError, match='unavailable case facts: scale'):
        driver.answer_question(tmp_path, {'question': question, 'allowFreeText': free_text,
            '_deferred_fact_fields': ['vpc_id']}, {'goal': '先规划，实现阶段再询问 VpcId'}, {}, {})


def test_missing_resource_name_diagnostic_preserves_required_existing_identity_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['goal'], 'missing_fields': ['other'], 'missing_detail': 'resource_name'})
    diagnostics = {}
    with pytest.raises(RuntimeError, match='unavailable case facts: other'):
        driver.answer_question(tmp_path, {'question': '杭州已有 OSS Bucket 名称是什么? private-secret'},
                               {'goal': '只规划杭州网络'}, {}, diagnostics)
    assert diagnostics['question_driver_name_subject_present'] is True
    assert diagnostics['question_driver_existing_resource_requested'] is True
    assert diagnostics['question_driver_new_resource_requested'] is False
    assert diagnostics['question_driver_question_resource_kinds'] == ['oss']
    assert 'private' not in str(diagnostics)


@pytest.mark.parametrize('question', [
    '请确认你要在哪个云厂商新建 VPC：AWS、阿里云还是其他云？',
    '请确认云厂商 AWS；你要创建的 VPC 名称是什么？',
    '请提供已有 VPC ID，再确认云厂商 AWS。',
    '请确认云厂商 AWS，并提供 NoEcho Password。',
])
def test_unrelated_name_advice_does_not_block_cloud_vendor_answer(tmp_path, monkeypatch, question):
    monkeypatch.setattr(driver, '_select_facts', lambda *_: {
        'fact_keys': ['cloud_vendor', 'constraints', 'goal'], 'option_id': '',
        'missing_fields': ['other'], 'missing_detail': 'resource_name'})
    goal = '请为 AWS 账号创建一个 Amazon VPC，不使用阿里云，也不生成 ROS 模板。'
    facts = driver.case_facts(goal, {'cloud_vendor': 'AWS'})
    diagnostics = {}
    pending = {'question': question, 'allowFreeText': True}
    if any(marker in question for marker in ('名称', 'VPC ID', 'Password')):
        with pytest.raises(RuntimeError, match='unavailable case facts: other'):
            driver.answer_question(tmp_path, pending, facts, {}, diagnostics)
    else:
        answer, _ = driver.answer_question(tmp_path, pending, facts, {}, diagnostics)
        assert 'AWS' in answer and '不使用阿里云' in answer
        assert diagnostics['question_driver_unresolved_fields'] == ['other']
        assert diagnostics['question_driver_unknown_detail_restated_count'] == 1
