from __future__ import annotations

import json
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
