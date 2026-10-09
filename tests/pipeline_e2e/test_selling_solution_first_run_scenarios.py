from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml


def test_redaction_fixture_delegates_password_generation_without_supplying_secret(runner):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='redaction'), stack_name='iac-e2e-fake', cidr='10.0.0.0/24')
    prompt = runner._initial_prompt(runtime)
    assert 'NoEcho' in prompt
    assert '密码由你生成满足模板约束的随机值' in prompt
    assert '公开载荷中展示密码值' in prompt
    assert '只到部署确认，不创建资源' in prompt


def _runner_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "pipeline" / "e2e" / "selling_solution_first" / "run_scenarios.py"
    spec = importlib.util.spec_from_file_location("selling_solution_first_real_e2e", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_terminal_origin_diagnostic_preserves_failure_without_exporting_private_text(runner):
    origins: set[str] = set()
    assert runner._has_unhandled_terminal_error({'error': {'code': -32603, 'message':
        'Traceback (most recent call last): private-data'}}, origins)
    assert origins == {'rpc_error'}
    origins.clear()
    assert runner._has_unhandled_terminal_error({'role': 'assistant', 'content':
        'Traceback (most recent call last): private-data'}, origins)
    assert origins == {'assistant_text'}
    assert 'private-data' not in str(origins)


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    return _runner_module()


@pytest.mark.parametrize(('depends_on_old', 'expected_count'), [(False, 0), (True, 1)])
def test_cleanup_dependency_probe_uses_accepted_stacks_and_exports_no_identity(
    runner, tmp_path, monkeypatch, capsys, depends_on_old, expected_count,
):
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
    from scripts.repl.e2e import run_pipeline_scenarios as repl

    receipts = [{'stackId': s, 'stackName': s + '-private-name', 'regionId': 'cn-hangzhou',
                 'ownershipSource': 'accepted_create_ledger'} for s in ('old-private-stack', 'new-private-stack')]
    manifest = tmp_path / 'private-input.json'
    manifest.write_text(json.dumps({'resources': receipts, 'old_stack_id': receipts[0]['stackId'],
        'new_stack_ids': [receipts[1]['stackId']], 'fixture_vpc_id': 'independent-private-vpc'}), encoding='utf-8')
    calls = []
    def get_stack(request):
        calls.append(('get', request.stack_id))
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {'StackName': request.stack_id + '-private-name'}))
    def list_resources(request):
        calls.append(('list', request.stack_id))
        old = request.stack_id == receipts[0]['stackId']
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {'Resources': [{
            'ResourceType': 'ALIYUN::VPC::VPC' if old else 'ALIYUN::ECS::SecurityGroup',
            'PhysicalResourceId': 'old-private-vpc' if old else 'sg-private-owned', 'Status': 'CREATE_COMPLETE'}]}))
    monkeypatch.setattr(cloud_credentials, 'CloudCredentials',
                        lambda: SimpleNamespace(get_provider=lambda name: object()))
    monkeypatch.setattr(RosClientFactory, 'create', lambda *a: SimpleNamespace(
        get_stack=get_stack, list_stack_resources=list_resources))
    def read_group(product, action, params):
        assert product == 'ecs' and action == 'DescribeSecurityGroupAttribute'
        assert params == {'RegionId': 'cn-hangzhou', 'SecurityGroupId': 'sg-private-owned'}
        return {'SecurityGroupId': 'sg-private-owned',
                'VpcId': 'old-private-vpc' if depends_on_old else 'independent-private-vpc'}
    monkeypatch.setattr(repl, '_call_aliyun_api', read_group)
    monkeypatch.setattr(sys, 'argv', ['probe', str(manifest)])
    exec(runner._CLOUD_CLEANUP_DEPENDENCY_PROBE_CODE, {})
    result = json.loads(capsys.readouterr().out)
    assert result['new_group_depends_on_old_vpc_count'] == expected_count
    assert result['fixture_is_old_vpc'] is False
    assert result['owned_stacks'] == 2
    assert {stack for _, stack in calls} == {r['stackId'] for r in receipts}
    assert 'private' not in json.dumps(result)


def test_cleanup_dependency_probe_blocks_missing_ownership_before_cloud_reads(runner, tmp_path, monkeypatch, capsys):
    from iac_code.services import cloud_credentials

    manifest = tmp_path / 'input.json'
    manifest.write_text(json.dumps({'resources': [{'stackId': 'private-unowned'}],
                                   'old_stack_id': 'private-unowned', 'new_stack_ids': []}), encoding='utf-8')
    monkeypatch.setattr(cloud_credentials, 'CloudCredentials', lambda: pytest.fail('unowned cloud query'))
    monkeypatch.setattr(sys, 'argv', ['probe', str(manifest)])
    with pytest.raises(SystemExit):
        exec(runner._CLOUD_CLEANUP_DEPENDENCY_PROBE_CODE, {})
    assert json.loads(capsys.readouterr().out) == {'unavailable_stage': 'ownership'}


def test_generated_image_request_updates_clarification_facts_without_text_substitution(runner, monkeypatch):
    sent = []
    runtime = SimpleNamespace(current_goal='generic network application deployment',
                              spec=SimpleNamespace(profile='multimodal_lifecycle'), cidr='10.0.1.0/24')
    pty = SimpleNamespace(drain_output=lambda: None,
                          send=lambda value, **kw: sent.append(('enter', value)))
    monkeypatch.setattr(runner, '_repl_paste_generated_image',
                        lambda rt, pt, key, text: sent.append(('image', text)))
    monkeypatch.setattr(runner, '_repl_focus_confirmation_input', lambda *a: None)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    initial = '在阿里云杭州使用已有 VPC 创建 VSwitch；不创建 ECS；VpcId 必须由我确认'
    runner._repl_submit_generated_image(runtime, pty, 'initial', initial, label='initial-image')
    assert runner._question_facts(runtime)['goal'] == initial
    assert 'generic network' not in json.dumps(runner._question_facts(runtime))
    rollback = '我改需求了：使用已有 VPC 创建安全组，不创建 VSwitch'
    runner._repl_choose_direct_image(runtime, pty, 'rollback-interrupt', rollback)
    assert runner._question_facts(runtime)['goal'] == rollback
    assert sent == [('image', initial), ('enter', '\r'), ('image', rollback), ('enter', '\r')]


def test_aws_early_exit_never_uses_alibaba_fixture_facts(runner, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='early_exit'), cidr='10.0.1.0/24',
                              question_facts={'vpc_id': 'vpc-alibaba', 'region': 'cn-hangzhou'})
    monkeypatch.setattr(runner, 'network_facts', lambda *a: pytest.fail('AWS cannot use an Alibaba fixture'))
    facts = runner._question_facts(runtime)
    assert facts['cloud_vendor'] == 'AWS'
    assert 'vpc_id' not in facts and 'region' not in facts and 'cidr' not in facts
    assert runner._resolve_runtime_question_facts(runtime, ('vpc_id', 'region', 'cloud_vendor')) == {}


def test_required_ids_are_withheld_in_step1_clarification_and_available_only_in_step2(runner, tmp_path, monkeypatch):
    seen = []
    runtime = SimpleNamespace(
        spec=SimpleNamespace(profile='step2_parameter'), paths=SimpleNamespace(config_dir=tmp_path),
        cidr='10.0.1.0/24', diagnostics={},
        args=SimpleNamespace(cleanup_vpc_id='vpc-actual-fixture', cleanup_zone_id='cn-hangzhou-i'),
    )
    def answer(_config, pending, facts, *args, **kw):
        seen.append(facts)
        if pending['_step_id'] == runner.NEW_STEPS[0]:
            assert kw['fact_resolver'](('vpc_id', 'zone_id')) == {}
        return facts['goal'], 'goal'
    monkeypatch.setattr(runner, 'answer_question', answer)
    monkeypatch.setattr(runner, 'network_facts', lambda *a: pytest.fail('fixture IDs already supplied'))
    runner._answer_runtime_question(runtime, {'question': '先确认需求?', '_step_id': runner.NEW_STEPS[0]})
    assert 'vpc_id' not in seen[0] and 'zone_id' not in seen[0]
    assert '必须逐项分别询问' in seen[0]['goal']
    runner._answer_runtime_question(runtime, {'question': 'VpcId?', '_step_id': runner.NEW_STEPS[1]})
    assert seen[1]['vpc_id'] == 'vpc-actual-fixture'
    assert seen[1]['zone_id'] == 'cn-hangzhou-i'


def test_multimodal_question_wait_ignores_terminal_prompt_without_native_question(runner, tmp_path, monkeypatch):
    pipeline = tmp_path / 'projects/p/s/pipeline'
    pipeline.mkdir(parents=True)
    meta = pipeline / 'meta.yaml'
    display = pipeline / 'display.jsonl'
    display.write_text(json.dumps({'type': 'candidate_selection_submitted'}) + '\n', encoding='utf-8')
    sent = []
    drains = []
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1.0), checks={}, repl_confirmation_wait_count=0)
    confirmation = {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
                    'payload': {'kind': 'deployment_confirmation', 'options': [{'value': 'cancel'}]}}

    class Pty:
        events = []
        transcript = 'An example prompt:  > '

        def drain_output(self):
            drains.append(True)
            if len(drains) == 2:
                meta.write_text(yaml.safe_dump({'current_step': runner.NEW_STEPS[1], 'execution': {
                    'pending_input_kind': 'ask_user_question', 'pending_ask_user_question_input': {
                        'toolUseId': 'real-question', 'question': 'Which VPC?', 'allowFreeText': True,
                    },
                }}), encoding='utf-8')

        def expect_any(self, *args, **kwargs):
            return runner.REPL_ASK_INPUT_READY_PATTERNS[0]

        def paste_image_fixture(self, key, **kwargs):
            assert len(drains) >= 2
            assert runner._pending_repl_parameter_question(runtime, set()) is not None
            sent.append(key)

        def send(self, text, **kwargs):
            assert text == '\r'
            meta.write_text(yaml.safe_dump({'current_step': runner.NEW_STEPS[1], 'execution': {}}), encoding='utf-8')
            with display.open('a', encoding='utf-8') as output:
                output.write(json.dumps(confirmation) + '\n')

    monkeypatch.setattr(runner, '_observe_repl_wait', lambda *args, **kwargs: False)
    runner._repl_wait_multimodal_confirmation(runtime, Pty(), primary_image_key='ask-first-answer', phase='initial')
    assert sent == ['ask-first-answer']
    assert runtime.checks['initial image question 1 acknowledged'] is True
    assert runtime.repl_confirmation_wait_count == 1


@pytest.mark.parametrize('terminal', [None, 'pipeline_failed', 'pipeline_completed'])
def test_multimodal_wait_without_native_input_fails_without_sending_image(runner, tmp_path, monkeypatch, terminal):
    pipeline = tmp_path / 'projects/p/s/pipeline'
    pipeline.mkdir(parents=True)
    if terminal:
        (pipeline / 'display.jsonl').write_text(json.dumps({'type': terminal}) + '\n', encoding='utf-8')
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=0.01), checks={})
    pty = SimpleNamespace(transcript='An example:  > ', events=[], drain_output=lambda: None,
                          paste_image_fixture=lambda *a: pytest.fail('no native question to answer'),
                          expect_any=lambda *a, **kw: pytest.fail('no native input reader'))
    monkeypatch.setattr(runner, '_observe_repl_wait', lambda *a, **kw: False)
    error = RuntimeError if terminal else TimeoutError
    with pytest.raises(error, match='terminal display event' if terminal else 'timed out waiting'):
        runner._repl_wait_multimodal_confirmation(runtime, pty, primary_image_key='ask-first-answer', phase='initial')
    if not terminal:
        assert runtime.checks['REPL display user_input_required occurrence 1 observed'] is False


def test_multimodal_confirmation_ignores_confirmation_before_latest_selection(runner, tmp_path, monkeypatch):
    pipeline = tmp_path / 'projects/p/s/pipeline'
    pipeline.mkdir(parents=True)
    confirmation = {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
                    'payload': {'kind': 'deployment_confirmation', 'options': [{'value': 'cancel'}]}}
    display = pipeline / 'display.jsonl'
    display.write_text(json.dumps(confirmation) + '\n' +
                       json.dumps({'type': 'candidate_selection_submitted'}) + '\n', encoding='utf-8')
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1.0), checks={}, repl_confirmation_wait_count=0)
    drains = []
    def drain():
        drains.append(True)
        if len(drains) == 2:
            with display.open('a', encoding='utf-8') as output:
                output.write(json.dumps(confirmation) + '\n')
    pty = SimpleNamespace(transcript='', events=[], drain_output=drain,
                          paste_image_fixture=lambda *a: pytest.fail('not a parameter question'))
    monkeypatch.setattr(runner, '_observe_repl_wait', lambda *a, **kw: False)
    runner._repl_wait_multimodal_confirmation(runtime, pty, primary_image_key='rollback-ask-answer', phase='rollback')
    assert len(drains) >= 2
    assert runtime.repl_confirmation_wait_count == 2
    assert pty.events[-1]['occurrence'] == 2


def test_image_question_does_not_resend_while_original_question_is_unacknowledged(runner, tmp_path):
    meta = tmp_path / 'projects/p/s/pipeline/meta.yaml'
    meta.parent.mkdir(parents=True)
    meta.write_text(yaml.safe_dump({'current_step': runner.NEW_STEPS[1], 'execution': {
        'pending_input_kind': 'ask_user_question', 'pending_ask_user_question_input': {
            'toolUseId': 'native-question', 'question': 'Which VPC?', 'allowFreeText': True,
        },
    }}), encoding='utf-8')
    sent = []
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=0.01), checks={})
    pty = SimpleNamespace(events=[], drain_output=lambda: None,
                          expect_any=lambda *a, **kw: runner.REPL_ASK_INPUT_READY_PATTERNS[0],
                          paste_image_fixture=lambda key, **kwargs: sent.append(key), send=lambda *a, **kw: None)
    with pytest.raises(TimeoutError, match='answer acknowledgement'):
        runner._repl_wait_multimodal_confirmation(
            runtime, pty, primary_image_key='ask-first-answer', phase='initial',
        )
    assert sent == ['ask-first-answer']
    assert runtime.checks['initial image question 1 acknowledged'] is False


def test_server_startup_failure_keeps_child_tracked_and_emits_only_fixed_diagnostics(runner, tmp_path):
    import subprocess
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    runtime = object.__new__(runner.ScenarioRuntime)
    runtime.processes = []
    runtime.diagnostics = {}
    prefix = tmp_path / 'server-1'
    prefix.with_suffix('.stderr.log').write_text('OSError: address already in use; private-secret', encoding='utf-8')
    class Harness:
        server = SimpleNamespace(process=process, _log_prefix=prefix)
        def start_server(self):
            raise RuntimeError('not ready')
    h = Harness()
    runner._track_a2a_server_processes(runtime, h)
    try:
        with pytest.raises(RuntimeError, match='not ready'):
            h.start_server()
        assert runtime.processes == [process]
        assert runtime.diagnostics == {'server_startup_process_alive': True,
                                       'server_startup_error_types': ['OSError'], 'server_startup_port_in_use': True}
        assert 'private-secret' not in json.dumps(runtime.diagnostics)
    finally:
        runtime.terminate_processes()


def test_runtime_instructions_keep_isolation_without_imposing_stack_names(runner, tmp_path):
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path), env={},
                              owned_stack_names={"iac-e2e-fixture-main"})
    runner._write_runtime_identity_instructions(runtime)
    instruction = (tmp_path / runtime.env["IAC_CODE_INSTRUCTION_MEMORY_FILE"]).read_text(encoding="utf-8")
    assert "iac-e2e-fixture-main" not in instruction
    assert "不得复用已有 Stack" in instruction
    assert "删除本次测试之外的资源" in instruction


def test_registry_has_exact_documented_45_cases(runner: ModuleType) -> None:
    assert len(runner.SCENARIOS) == 45
    assert len(runner.SCENARIO_BY_NAME) == 45
    assert [item.case_id for item in runner.SCENARIOS] == [
        *(f"A{index:02d}" for index in range(1, 28)),
        *(f"R{index:02d}" for index in range(1, 15)),
        "W01",
        "W02",
        "D01",
        "L01",
    ]
    assert sum(item.surface is runner.Surface.A2A for item in runner.SCENARIOS) == 27
    assert sum(item.surface is runner.Surface.REPL for item in runner.SCENARIOS) == 14
    assert sum(item.surface is runner.Surface.WEB for item in runner.SCENARIOS) == 2
    assert sum(item.surface is runner.Surface.DESKTOP for item in runner.SCENARIOS) == 1
    assert sum(item.surface is runner.Surface.LEGACY for item in runner.SCENARIOS) == 1


@pytest.mark.parametrize(
    ("suite", "expected_ids"),
    [
        ("smoke", ["A01", "R01", "W01"]),
        ("core", [*(f"A{i:02d}" for i in range(1, 9)), *(f"R{i:02d}" for i in range(1, 7))]),
        ("recovery", [*(f"A{i:02d}" for i in range(9, 24)), *(f"R{i:02d}" for i in range(7, 14))]),
        ("multimodal", ["A25", "A26", "A27", "R14", "W02"]),
        ("safety", ["A02", "A10", "A11", "A18", "A22", "A23", "A24", "D01", "L01"]),
        ("web", ["W01", "W02"]),
        ("desktop", ["D01"]),
        ("legacy", ["L01"]),
        ("all", []),
    ],
)
def test_suite_membership_matches_design(runner: ModuleType, suite: str, expected_ids: list[str]) -> None:
    if suite == "all":
        expected_ids = [item.case_id for item in runner.SCENARIOS]
    assert [item.case_id for item in runner.scenarios_for_suite(suite)] == expected_ids


def test_selection_deduplicates_and_keeps_registry_order(runner: ModuleType) -> None:
    selected = runner.select_scenarios(["web-full-flow", "a2a-happy-multi-plan"], ["smoke", "web"])
    assert [item.case_id for item in selected] == ["A01", "R01", "W01", "W02"]


def test_parser_defaults_to_concurrency_three_and_smoke(runner: ModuleType) -> None:
    args = runner.parse_args([])
    assert args.concurrency == 3
    assert args.cidr_pool == ""
    assert [item.case_id for item in runner.select_scenarios(args.scenario, args.suite)] == ["A01", "R01", "W01"]
    assert runner.parse_args(["--cidr-pool", "10.250.4.0/22"]).cidr_pool == "10.250.4.0/22"
    with pytest.raises(SystemExit):
        runner.parse_args(["--concurrency", "0"])


def test_run_dir_and_cloud_write_validation(runner: ModuleType, tmp_path: Path) -> None:
    args = runner.parse_args(
        [
            "--scenario",
            "a2a-happy-multi-plan",
            "--run-dir",
            str(tmp_path),
            "--allow-real-cloud",
        ]
    )
    selected = runner.select_scenarios(args.scenario, args.suite)
    with pytest.raises(ValueError, match="--run-dir"):
        runner.validate_args(args, selected)
    args.concurrency = 1
    with pytest.raises(ValueError, match="--allow-cloud-write"):
        runner.validate_args(args, selected)
    args.allow_cloud_write = True
    runner.validate_args(args, selected)


def test_credentials_are_copied_with_safe_modes_and_source_is_unchanged(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "case" / "config"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text(f"fake-{name}\n", encoding="utf-8")
    (source / "settings.yml").write_text("provider: fake\n", encoding="utf-8")
    before = runner.snapshot_credentials(source)
    audit = runner.copy_credentials(source, destination, inherit_settings=True)
    after = runner.snapshot_credentials(source)

    assert audit.credential_files_copied
    assert audit.settings_copied
    assert audit.directory_mode_ok
    assert audit.file_modes_ok
    assert audit.independent_files
    assert runner.credential_snapshot_unchanged(before, after)
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for name in (*runner.CREDENTIAL_FILES, "settings.yml"):
        target = destination / name
        assert not target.is_symlink()
        if os.name != "nt":
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == (source / name).read_text(encoding="utf-8")


def test_credentials_reject_symlink_source(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    real = tmp_path / "credential"
    real.write_text("fake", encoding="utf-8")
    (source / runner.CREDENTIAL_FILES[0]).symlink_to(real)
    (source / runner.CREDENTIAL_FILES[1]).write_text("fake", encoding="utf-8")
    with pytest.raises(ValueError, match="non-symlink"):
        runner.copy_credentials(source, tmp_path / "config", inherit_settings=False)


def test_public_noecho_parameter_values_must_be_redacted(runner: ModuleType, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    template = workspace / "templates" / "database.yml"
    template.parent.mkdir(parents=True)
    template.write_text(
        """ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  MasterUserPassword:
    Type: String
    NoEcho: true
Resources: {}
""",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(paths=argparse.Namespace(workspace_dir=workspace, run_dir=tmp_path))

    assert runner._public_noecho_values_are_redacted(
        runtime,
        [{"parameter_name": "MasterUserPassword", "actual_value": "<redacted>"}],
    )
    assert not runner._public_noecho_values_are_redacted(
        runtime,
        [{"parameter_name": "MasterUserPassword", "actual_value": "Fake-test-password-9!"}],
    )


def test_repl_cloud_discovery_reads_persisted_tool_transcript(runner: ModuleType, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    transcript = (
        config_dir
        / "projects"
        / "project"
        / "session"
        / "pipeline"
        / "transcripts"
        / "transcript_att_0001"
        / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    owned_name = "iac-e2e-ssf-repl-single-plan-happy-abc12345"
    transcript.write_text(
        json.dumps(
            {
                "tool_result": {
                    "stack_id": "test-stack-id-123456",
                    "stack_name": owned_name,
                    "region_id": "cn-hangzhou",
                }
            }
        )
        + "\n"
        + json.dumps(
            {
                "tool_result": {
                    "stack_id": "unowned-stack-id-123456",
                    "stack_name": "somebody-elses-stack",
                    "region_id": "cn-hangzhou",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(
            run_dir=tmp_path,
            config_dir=config_dir,
            artifacts_dir=tmp_path / "artifacts",
        ),
        owned_stack_names={owned_name},
        cloud_resources=[],
    )

    # An unbound flat result and a generated name cannot prove creation ownership.
    assert runner.discover_cloud_resources(runtime) == []
    assert json.loads((tmp_path / "cloud-resources.json").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("proof", [True, False])
def test_cleanup_requires_creation_receipt_and_never_discovers_by_name(runner, tmp_path, monkeypatch, proof):
    for name in ("artifacts", "logs"):
        (tmp_path / name).mkdir()
    runtime = SimpleNamespace(
        args=SimpleNamespace(python=sys.executable, stream_timeout=30, skip_final_teardown=False),
        paths=SimpleNamespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts", logs_dir=tmp_path / "logs"),
        env={}, checks={}, cloud_resources=[],
    )
    resource = {"provider": "ros", "resourceType": "stack", "stackId": "created-stack-id",
                "stackName": "model-chosen-name", "regionId": "cn-hangzhou", "createdByCase": "true"}
    if proof:
        resource["ownershipSource"] = "accepted_create_ledger"
    monkeypatch.setattr(runner, "discover_cloud_resources", lambda _: [resource])
    calls = []
    def fake_run(command, **kwargs):
        assert command[2] == runner._CLOUD_CLEANUP_CODE
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{"deleted": true}', '')
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.cleanup_cloud_resources(runtime) == ("completed" if proof else "failed")
    assert bool(calls) is proof
    assert runtime.checks["test-owned stacks cleaned"] is proof


@pytest.mark.parametrize("code", ["EntityNotExist.Stack", "NotFound.Stack", "StackNotFound"])
@pytest.mark.parametrize("phase", ["get", "delete"])
def test_cleanup_accepts_stack_disappearance_during_get_or_delete(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    code: str, phase: str,
) -> None:
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun import ros_client

    manifest = tmp_path / "stack.json"
    manifest.write_text(json.dumps({"stackId": "test-stack", "stackName": "iac-e2e-test", "regionId": "cn-hangzhou"}),
                        encoding="utf-8")

    class StackMissingError(Exception):
        pass

    missing = StackMissingError(code)
    missing.code = code

    class Client:
        def get_stack(self, _request):
            if phase == "get":
                raise missing
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                "StackName": "iac-e2e-test", "Status": "CREATE_COMPLETE",
            }))

        def delete_stack(self, _request):
            raise missing

    monkeypatch.setattr(cloud_credentials, "CloudCredentials", lambda: SimpleNamespace(
        get_provider=lambda _: SimpleNamespace(region_id="cn-hangzhou"),
    ))
    monkeypatch.setattr(ros_client.RosClientFactory, "create", lambda *_args: Client())
    monkeypatch.setattr(sys, "argv", ["cleanup", str(manifest)])
    with pytest.raises(SystemExit) as exited:
        exec(runner._CLOUD_CLEANUP_CODE, {})
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": True, "notFound": True}


def test_cleanup_never_confuses_missing_credentials_or_unowned_stack_with_deletion(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun import ros_client

    manifest = tmp_path / "stack.json"
    manifest.write_text(json.dumps({"stackId": "test-stack", "stackName": "iac-e2e-test"}), encoding="utf-8")
    monkeypatch.setattr(cloud_credentials, "CloudCredentials", lambda: SimpleNamespace(
        get_provider=lambda _: SimpleNamespace(region_id="cn-hangzhou"),
    ))
    monkeypatch.setattr(sys, "argv", ["cleanup", str(manifest)])
    denied = RuntimeError("InvalidAccessKeyId.NotFound: access key not found")
    client = SimpleNamespace(get_stack=lambda _: (_ for _ in ()).throw(denied))
    monkeypatch.setattr(ros_client.RosClientFactory, "create", lambda *_args: client)
    with pytest.raises(RuntimeError, match="InvalidAccessKeyId"):
        exec(runner._CLOUD_CLEANUP_CODE, {})
    client.get_stack = lambda _: SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
        "StackName": "not-owned", "Status": "CREATE_COMPLETE",
    }))
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        exec(runner._CLOUD_CLEANUP_CODE, {})


def test_runtime_defaults_follow_real_settings_shape(runner: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "settings.yml").write_text(
        "activeProvider: openai_compatible\n"
        "providers:\n"
        "  openai_compatible:\n"
        "    model: test-model\n"
        "    apiBase: https://example.invalid/v1\n",
        encoding="utf-8",
    )
    assert runner.read_runtime_defaults(tmp_path) == {
        "provider": "openai_compatible",
        "model": "test-model",
        "api_base": "https://example.invalid/v1",
    }


def test_step1_clarification_answer_supplies_the_missing_product_intent(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=runner.SCENARIO_BY_NAME["a2a-step1-clarify"],
        cidr="10.250.0.0/24",
        args=argparse.Namespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )

    plan = runner._a2a_plan(runtime)

    assert len(plan.ask_answers) == 1
    assert "Node.js 电商后端 API" in plan.ask_answers[0]
    assert "cn-hangzhou" in plan.ask_answers[0]


def test_a2a_multimodal_plan_uses_distinct_images_then_plain_text(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=runner.SCENARIO_BY_NAME["a2a-image-asks-confirmation"],
        cidr="10.250.0.0/24",
        args=argparse.Namespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )
    plan = runner._a2a_plan(runtime)

    first_ask = runner._a2a_response_for_pending(runtime, "ask_user_question", plan)
    second_ask = runner._a2a_response_for_pending(runtime, "ask_user_question", plan)
    first_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)
    second_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)

    assert first_ask[1] == "ask-first-answer"
    assert second_ask[1] == "ask-second-answer"
    assert first_confirmation[1] == "confirmation-adjust"
    assert second_confirmation[1] == ""
    assert "调整参数" in first_confirmation[0]
    assert json.loads(second_confirmation[0])["action"] == "cancel"


def test_a2a_image_questions_keep_step2_answer_and_image_when_step1_reasks(runner: ModuleType) -> None:
    runtime = SimpleNamespace(
        spec=runner.SCENARIO_BY_NAME["a2a-image-asks-confirmation"], cidr="10.250.0.0/24",
        stack_name="iac-e2e-image-asks-test", args=SimpleNamespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )
    plan = runner._a2a_plan(runtime)
    first = runner._a2a_response_for_pending(runtime, "ask_user_question", plan, runner.NEW_STEPS[0])
    repeated = runner._a2a_response_for_pending(runtime, "ask_user_question", plan, runner.NEW_STEPS[0])
    parameter = runner._a2a_response_for_pending(runtime, "ask_user_question", plan, runner.NEW_STEPS[1])
    assert first[1] == "ask-first-answer"
    assert repeated == (first[0], "")
    assert parameter[1] == "ask-second-answer"
    assert "CidrBlock 使用 10.250.0.0/25" in parameter[0]
    assert "阿里云杭州" in runner._initial_prompt(runtime)
    assert "user_required" in runner._initial_prompt(runtime)
    # The missing input must really come from the user. CIDRs are normally
    # auto-solvable, so they cannot reliably establish this question boundary.
    assert 'CidrBlock 是 user_required' not in runner._initial_prompt(runtime)
    assert 'ExternalProjectCode' in runner._initial_prompt(runtime)
    assert '公司外部业务系统' in runner._initial_prompt(runtime)
    assert 'ExternalProjectCode' in first[0] and '尚未提供' in first[0]
    assert 'partner-project-e2e-node-api' not in first[0]
    assert '10.250.0.0' not in first[0]
    assert 'ExternalProjectCode' in parameter[0] and 'partner-project-e2e-node-api' in parameter[0]


def test_a2a_image_acceptance_rejects_early_exit_and_requires_complete_adjustment(
    runner: ModuleType, tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(paths=SimpleNamespace(run_dir=tmp_path), events_path=tmp_path / "events.jsonl")

    def event(event_type, step, **data):
        return {"eventType": event_type, "step": {"id": step}, "data": data}

    step1, step2 = runner.NEW_STEPS[:2]
    asks = [event("input_received", step, kind="ask_user_question") for step in (step1, step2)]
    for name, answer in zip(("turn-step1", "turn-step2"), asks):
        (tmp_path / f"{name}.events.jsonl").write_text(json.dumps(answer), encoding="utf-8")
    runtime.events_path.write_text("\n".join(json.dumps({
        "type": "a2a-turn-started", "name": name, "image": True,
    }) for name in ("turn-step1", "turn-step2")), encoding="utf-8")
    early_exit = [asks[0], event("pipeline_completed", step1)]
    assert not all(runner._a2a_image_asks_checks(runtime, early_exit).values())
    complete = [
        *asks,
        event("input_received", step2, kind="deployment_confirmation", has_images=True, structured=False),
        event("tool_started", step2, toolName="ros_preview_template"),
        event("tool_started", step2, toolName="ros_estimate_template_cost"),
        event("input_required", step2, kind="deployment_confirmation"),
        event("input_received", step2, kind="deployment_confirmation", action="cancel"),
        event("pipeline_completed", step2),
    ]
    assert all(runner._a2a_image_asks_checks(runtime, complete).values())
    no_quote = [item for item in complete if item["data"].get("toolName") != "ros_estimate_template_cost"]
    assert runner._a2a_image_asks_checks(runtime, no_quote)["image adjustment reran Preview and quote"] is False
    attempted_deploy = [*complete, event("tool_started", runner.NEW_STEPS[2], toolName="ros_deploy")]
    assert runner._a2a_image_asks_checks(runtime, attempted_deploy)[
        "image adjustment was canceled without deployment"
    ] is False
    runtime.events_path.write_text(json.dumps({
        "type": "a2a-turn-started", "name": "turn-step1", "image": True,
    }), encoding="utf-8")
    assert runner._a2a_image_asks_checks(runtime, complete)[
        "Step 2 parameter question accepted an image answer"
    ] is False


def test_a2a_image_interrupt_only_uses_rollback_image_once(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=runner.SCENARIO_BY_NAME["a2a-image-interrupt-handoff"],
        cidr="10.250.0.0/24",
        stack_name="iac-e2e-image-interrupt-test",
        args=argparse.Namespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )
    plan = runner._a2a_plan(runtime)

    first_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)
    second_confirmation = runner._a2a_response_for_pending(runtime, "deployment_confirmation", plan)

    assert first_confirmation[1] == "rollback-interrupt"
    assert "StackName" not in first_confirmation[0]
    assert second_confirmation[1] == ""
    assert json.loads(second_confirmation[0])["action"] == "confirm"


def test_a2a_image_interrupt_instruction_keeps_target_inside_image(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="image_interrupt"),
        event=lambda *args, **kwargs: None,
        stack_name="iac-e2e-ssf-a2a-image-interrupt-handoff-abc12345",
    )
    calls: list[dict[str, str]] = []

    class Harness:
        def stream_image_text(self, **kwargs):
            calls.append(kwargs)
            return argparse.Namespace(context_id="ctx", task_id="task", last_input_required_step_id="")

    runner._a2a_turn(
        runtime, Harness(), prompt="create security group", name="interrupt", image_key="rollback-interrupt"
    )

    assert calls[0]["text"] == "create security group"
    assert "security group" not in calls[0]["prompt"].lower()
    assert "不是确认部署" in calls[0]["prompt"]
    assert "StackName" not in calls[0]["prompt"]


def test_backup_window_reads_pending_input_from_prepublication_snapshot(runner: ModuleType) -> None:
    state = {
        "snapshot": {
            "status": "waiting_input",
            "pendingInput": {
                "kind": "ask_user_question",
                "step": {"id": runner.NEW_STEPS[1]},
                "options": [
                    {"id": "use-default", "label": "使用默认值"},
                    {"id": "vpc-unit123", "label": "测试 VPC"},
                ],
            },
        }
    }

    step_id, kind, pending = runner._pending_from_pipeline_state(state)

    assert step_id == runner.NEW_STEPS[1]
    assert kind == "ask_user_question"
    assert runner._first_pending_resource_option_id_from_data(pending) == "vpc-unit123"


def test_backup_window_normalizes_candidate_select_snapshot_kind(runner: ModuleType) -> None:
    state = {
        "snapshot": {
            "pendingInput": {
                "kind": "candidate_select",
                "step": {"id": runner.NEW_STEPS[0]},
            }
        }
    }

    assert runner._pending_from_pipeline_state(state)[:2] == (runner.NEW_STEPS[0], "candidate_selection")


def test_backup_window_next_pending_must_follow_consumed_sequence(runner: ModuleType) -> None:
    class FakeA2A:
        @staticmethod
        def _extract_pipeline_envelopes(event: object) -> list[dict[str, object]]:
            assert isinstance(event, dict)
            return event["envelopes"]  # type: ignore[return-value]

    replayed = {"envelopes": [{"eventType": "input_required", "sequence": 10}]}
    advanced = {"envelopes": [{"eventType": "input_required", "sequence": 12}]}

    predicate = runner._input_required_after_sequence(FakeA2A(), 11)

    assert predicate(replayed, None) is False
    assert predicate(advanced, None) is True


def test_backup_window_pending_input_must_follow_sequence_and_match_identity(runner: ModuleType) -> None:
    class FakeA2A:
        @staticmethod
        def _extract_pipeline_envelopes(event: object) -> list[dict[str, object]]:
            assert isinstance(event, dict)
            return event["envelopes"]  # type: ignore[return-value]

    predicate = runner._input_required_after_sequence_kind_and_step(
        FakeA2A(),
        10,
        runner.NEW_STEPS[0],
        "candidate_selection",
    )
    prior_ask = {
        "envelopes": [
            {
                "eventType": "input_required",
                "sequence": 9,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "ask_user_question"},
            }
        ]
    }
    candidate = {
        "envelopes": [
            {
                "eventType": "input_required",
                "sequence": 11,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"},
            }
        ]
    }

    assert predicate(prior_ask, None) is False
    assert predicate(candidate, None) is True


def test_backup_window_consumed_input_must_follow_pending_sequence_and_match_identity(runner: ModuleType) -> None:
    class FakeA2A:
        @staticmethod
        def _extract_pipeline_envelopes(event: object) -> list[dict[str, object]]:
            assert isinstance(event, dict)
            return event["envelopes"]  # type: ignore[return-value]

    predicate = runner._input_received_after_sequence_kind_and_step(
        FakeA2A(),
        20,
        runner.NEW_STEPS[0],
        "candidate_selection",
    )
    replayed = {
        "envelopes": [
            {
                "eventType": "input_received",
                "sequence": 19,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"},
            }
        ]
    }
    wrong_kind = {
        "envelopes": [
            {
                "eventType": "input_received",
                "sequence": 21,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "ask_user_question"},
            }
        ]
    }
    consumed = {
        "envelopes": [
            {
                "eventType": "input_received",
                "sequence": 21,
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"},
            }
        ]
    }

    assert predicate(replayed, None) is False
    assert predicate(wrong_kind, None) is False
    assert predicate(consumed, None) is True


def test_backup_delay_uses_artifact_directory_for_multiple_windows(
    runner: ModuleType,
    tmp_path: Path,
) -> None:
    runtime = argparse.Namespace(paths=argparse.Namespace(artifacts_dir=tmp_path / "artifacts"))
    runtime.paths.artifacts_dir.mkdir()
    harness = argparse.Namespace(server_env={})
    a2a = argparse.Namespace(BACKUP_DELAY_FIXTURE_ROOT=tmp_path, BACKUP_DELAY_SECONDS=0.01)

    first = runner._arm_a2a_backup_delay(runtime, harness, a2a, 1)
    second = runner._arm_a2a_backup_delay(runtime, harness, a2a, 2)

    assert harness.server_env["IAC_CODE_E2E_BACKUP_DELAY_CONTROL"] == str(runtime.paths.artifacts_dir)
    assert runner._backup_delay_marker(first, "arm").is_file()
    assert runner._backup_delay_marker(second, "arm").is_file()


def test_backup_window_wait_reads_started_marker(
    runner: ModuleType, tmp_path: Path
) -> None:
    control = tmp_path / "control"
    runner._backup_delay_marker(control, "started").write_text("{}", encoding="utf-8")
    started = {"delaySeconds": 10}

    def wait_for_marker(_control: Path, marker: str, *, timeout: float) -> dict:
        assert marker == "started"
        assert timeout == 1.0
        return started

    runtime = argparse.Namespace(args=argparse.Namespace(timeout=240.0, stream_timeout=1800.0))
    a2a = argparse.Namespace(_wait_for_backup_delay_marker=wait_for_marker)
    stream = argparse.Namespace(events=[], done=False)

    assert runner._wait_a2a_backup_window_started(runtime, a2a, control, stream, 1) is started


def test_backup_window_wait_stops_when_stream_ends(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=1800.0))
    stream = argparse.Namespace(events=[], done=True)

    with pytest.raises(RuntimeError, match="stream ended before delay started"):
        runner._wait_a2a_backup_window_started(runtime, object(), tmp_path / "control", stream, 3)


def test_backup_window_wait_aborts_after_silent_stream(runner: ModuleType, tmp_path: Path, monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: now.__setitem__(0, now[0] + 601.0))
    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=1800.0), watchdog=None)
    stream = argparse.Namespace(events=[], done=False)

    with pytest.raises(TimeoutError, match="no stream progress"):
        runner._wait_a2a_backup_window_started(runtime, object(), tmp_path / "control", stream, 3)

    assert runtime.watchdog["state"] == "no_output"
    assert runtime.watchdog["waitingFor"] == "A2A backup delay marker"


@pytest.mark.parametrize("state", ["TASK_STATE_FAILED", "TASK_STATE_CANCELED"])
def test_unexpected_a2a_terminal_state_fails_immediately(runner: ModuleType, state: str) -> None:
    summary = argparse.Namespace(
        last_status_state=state, text="prior model output", terminal_status_text="pipeline_identity_mismatch"
    )

    with pytest.raises(RuntimeError, match=f"{state}.*pipeline_identity_mismatch"):
        runner._raise_for_unexpected_a2a_terminal(summary)


def test_unexpected_a2a_terminal_omits_prior_model_output(runner: ModuleType) -> None:
    summary = argparse.Namespace(last_status_state="TASK_STATE_FAILED", text="private prior model output")

    with pytest.raises(RuntimeError, match="TASK_STATE_FAILED$") as failure:
        runner._raise_for_unexpected_a2a_terminal(summary)
    assert "private prior model output" not in str(failure.value)


def test_continue_to_pending_stops_on_terminal_failure(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(paths=argparse.Namespace(run_dir=tmp_path))
    summary = argparse.Namespace(last_status_state="TASK_STATE_FAILED", terminal_status_text="execution conflict")
    harness = argparse.Namespace(stream=lambda **_kwargs: pytest.fail("must not start another A2A turn"))

    with pytest.raises(RuntimeError, match="execution conflict"):
        runner._continue_a2a_to_pending(
            runtime, harness, None, runner.A2AConversationPlan(), summary,
            "candidate_selection", name_prefix="recovery",
        )


def test_backup_restore_response_omits_stale_task_id(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = argparse.Namespace(
        name="first", last_status_state="TASK_STATE_INPUT_REQUIRED", last_input_required_step_id="step1",
        normal_handoff_ready=False,
    )
    finished = argparse.Namespace(name="done", last_status_state="TASK_STATE_COMPLETED", normal_handoff_ready=False)
    runtime = argparse.Namespace(
        cancel_event=threading.Event(), spec=argparse.Namespace(profile="backup_restore"),
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path), checks={},
    )
    harness = argparse.Namespace(context_id="context-1", pipeline_task_id="task-1")
    a2a = argparse.Namespace(_pipeline_completed=lambda summary: summary is finished)
    observed: list[str | None] = []
    monkeypatch.setattr(runner, "_pending_kind", lambda *_args: "candidate_selection")
    monkeypatch.setattr(runner, "_a2a_response_for_pending", lambda *_args: ("select", ""))

    def turn(_runtime: object, _harness: object, **kwargs: object) -> object:
        observed.append(kwargs.get("task_id"))
        return finished

    monkeypatch.setattr(runner, "_a2a_turn", turn)
    runner._continue_a2a_from_summary(
        runtime, harness, a2a, runner.A2AConversationPlan(), first,
        before_response=lambda *_args: True,
    )

    assert observed == [""]


def test_selling_repl_adapter_includes_wait_diagnosis_threshold(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        args=runner.parse_args([]),
        paths=argparse.Namespace(workspace_dir=tmp_path, run_dir=tmp_path),
        port=12345, env={}, cidr="10.0.0.0/24",
    )

    adapted = runner._python_namespace(runtime)

    assert adapted.wait_diagnosis_after == 120.0


def test_repl_waits_for_initial_prompt_before_sending_scenario_input(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    class FakePty:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def spawn(self) -> None:
            calls.append("spawn")

        def terminate(self) -> None:
            calls.append("terminate")

    fake_repl = argparse.Namespace(
        ReplPty=FakePty,
        _expect_initial_prompt=lambda _pty, _args: calls.append("ready"),
    )
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=1.0),
        env={},
        paths=argparse.Namespace(run_dir=tmp_path, workspace_dir=tmp_path),
        spec=argparse.Namespace(profile="happy_single"),
        event=lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner, "_repl_basic_flow", lambda _runtime, _pty: calls.append("scenario"))
    monkeypatch.setattr(runner, "_repl_wait_pipeline_completed", lambda _pty, _runtime: calls.append("terminal"))
    monkeypatch.setattr(runner, "_write_repl_artifacts", lambda _runtime, _pty, _repl: calls.append("artifacts"))

    runner._run_repl(runtime)

    assert calls == ["spawn", "ready", "scenario", "terminal", "terminate", "artifacts"]


def test_rollback_recovery_changes_goal_without_inventing_stack_name(runner: ModuleType) -> None:
    runtime = argparse.Namespace(stack_name="iac-e2e-ssf-owned-1234")

    prompt = runner._rollback_new_intent(runtime)

    assert "StackName" not in prompt


def test_walk_exposes_event_dicts_nested_directly_in_arrays(runner: ModuleType) -> None:
    event = {"batch": [{"eventType": "step_started", "step": {"id": runner.NEW_STEPS[1]}}]}

    assert any(isinstance(value, dict) and value.get("eventType") == "step_started" for _, value in runner._walk(event))
    assert runner._started_steps([event]) == []


def test_web_state_wait_reads_hydrated_status_endpoint(runner: ModuleType) -> None:
    requested_paths: list[str] = []

    class FakeWeb:
        @staticmethod
        def _session_path(session_id: str, suffix: str = "") -> str:
            return f"/api/sessions/{session_id}{suffix}"

        @staticmethod
        def _json_request(_base_url: str, _method: str, path: str) -> dict[str, object]:
            requested_paths.append(path)
            return {
                "status": "waiting_input",
                "pipeline": {"pendingInput": {"kind": "candidate_selection"}},
            }

    state = runner._wait_web_state(
        FakeWeb,
        "http://127.0.0.1:1",
        "web-session",
        lambda value: runner._web_pending_kind(value) == "candidate_selection",
        0.1,
    )

    assert runner._web_pending_kind(state) == "candidate_selection"
    assert requested_paths == ["/api/sessions/web-session/status"]


def test_web_state_wait_stops_immediately_on_pipeline_failure(runner: ModuleType) -> None:
    calls = 0

    class FakeWeb:
        @staticmethod
        def _session_path(session_id: str, suffix: str = "") -> str:
            return f"/api/sessions/{session_id}{suffix}"

        @staticmethod
        def _json_request(_base_url: str, _method: str, _path: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {
                "status": "idle",
                "pipeline": {
                    "snapshot": {
                        "status": "failed",
                        "normalHandoff": {
                            "status": "failed",
                            "outcome": "failed",
                            "action": "switch_to_normal",
                        },
                    }
                },
            }

    with pytest.raises(RuntimeError, match="terminal status 'failed'"):
        runner._wait_web_state(FakeWeb, "http://127.0.0.1:1", "web-session", lambda _value: False, 1800)

    assert calls == 1


def test_web_idle_waits_for_recovery_to_release_running_turn(runner: ModuleType) -> None:
    states = iter(
        [
            {
                "status": "running",
                "pipeline": {"pendingInput": {"kind": "deployment_confirmation"}},
            },
            {
                "status": "waiting_input",
                "pipeline": {"pendingInput": {"kind": "deployment_confirmation"}},
            },
        ]
    )

    class FakeWeb:
        @staticmethod
        def _session_path(session_id: str, suffix: str = "") -> str:
            return f"/api/sessions/{session_id}{suffix}"

        @staticmethod
        def _json_request(_base_url: str, _method: str, _path: str) -> dict[str, object]:
            return next(states)

    state = runner._wait_web_idle(FakeWeb, "http://127.0.0.1:1", "web-session", 1.0)

    assert state["status"] == "waiting_input"
    assert runner._web_pending_kind(state) == "deployment_confirmation"


def test_web_confirmation_boundary_accepts_repeated_parameter_questions(runner: ModuleType) -> None:
    for kind in ("ask_user_question", "deployment_confirmation"):
        assert runner._web_at_confirmation_boundary({"pipeline": {"snapshot": {"pendingInput": {"kind": kind}}}})

    assert not runner._web_at_confirmation_boundary(
        {"pipeline": {"snapshot": {"pendingInput": {"kind": "candidate_selection"}}}}
    )


def test_web_materialize_boundary_fails_fast_on_unexpected_rollback(runner: ModuleType) -> None:
    for kind in ("ask_user_question", "deployment_confirmation", "candidate_selection", "candidate_select"):
        assert runner._web_at_materialize_boundary({"pipeline": {"snapshot": {"pendingInput": {"kind": kind}}}})


def test_w02_parameter_answer_preserves_create_goal(runner: ModuleType) -> None:
    state = {
        "pipeline": {
            "waitingInput": {
                "kind": "ask_user_question",
                "data": {
                    "options": [
                        {"id": "use-existing-vswitch", "label": "直接使用已有交换机"},
                        {"id": "create-new-vswitch", "label": "改用不重叠网段新建交换机"},
                    ]
                },
            }
        }
    }

    answer = runner._web_w02_ask_answer(state)

    assert "create-new-vswitch" in answer
    assert "保持当前已选方案和部署目标不变" in answer
    assert "use-existing-vswitch" not in answer


def test_w02_parameter_answer_uses_exact_resource_option(runner: ModuleType) -> None:
    state = {
        "pipeline": {
            "snapshot": {
                "pendingInput": {
                    "kind": "ask_user_question",
                    "options": [
                        {"id": "vpc-unit123", "label": "测试 VPC"},
                        {"id": "vpc-unit456", "label": "备用 VPC"},
                    ],
                }
            }
        }
    }

    assert "vpc-unit123" in runner._web_w02_ask_answer(state)


def test_legacy_smoke_cancels_at_candidate_selection_without_selecting(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        checks={},
        cidr="10.250.0.0/24",
        spec=argparse.Namespace(profile="legacy_smoke", cloud_write=False),
    )
    runtime.paths.artifacts_dir.mkdir()

    class FakeHarness:
        context_id = "ctx-legacy"
        pipeline_task_id = "task-legacy"

        def __init__(self) -> None:
            self.canceled: list[str] = []

        def cancel_pipeline_task(self, name: str) -> dict[str, object]:
            self.canceled.append(name)
            return {"result": {"state": "canceled"}}

    class FakeA2A:
        @staticmethod
        def _latest_pending_kind(_path: Path) -> str:
            return "candidate_selection"

    harness = FakeHarness()
    monkeypatch.setattr(runner, "_initial_prompt", lambda _runtime: "legacy prompt")
    monkeypatch.setattr(
        runner,
        "_a2a_turn",
        lambda _runtime, _harness, **_kwargs: argparse.Namespace(name="legacy-initial"),
    )

    runner._run_a2a_legacy_smoke(runtime, harness, FakeA2A())

    assert harness.canceled == ["legacy-smoke-cancel-at-candidate-selection"]
    assert runtime.checks["legacy canceled at candidate selection"] is True
    assert json.loads((runtime.paths.artifacts_dir / "waiting-sequence.json").read_text(encoding="utf-8")) == [
        "candidate_selection"
    ]


def test_legacy_smoke_answers_clarification_before_canceling_at_candidate_selection(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        checks={},
        cidr="10.250.0.0/24",
        spec=argparse.Namespace(profile="legacy_smoke", cloud_write=False),
    )
    runtime.paths.artifacts_dir.mkdir()

    class FakeHarness:
        context_id = "ctx-legacy"
        pipeline_task_id = "task-legacy"

        def cancel_pipeline_task(self, _name: str) -> dict[str, object]:
            return {"result": {"state": "canceled"}}

    summaries = iter(
        [
            argparse.Namespace(name="legacy-initial", last_input_required_step_id="intent_parsing"),
            argparse.Namespace(name="legacy-candidate", last_input_required_step_id="confirm_and_select"),
        ]
    )
    prompts: list[str] = []

    def turn(_runtime, _harness, *, prompt: str, **_kwargs):
        prompts.append(prompt)
        return next(summaries)

    class FakeA2A:
        @staticmethod
        def _latest_pending_kind(path: Path) -> str:
            return "ask_user_question" if "legacy-initial" in path.name else "candidate_selection"

    monkeypatch.setattr(runner, "_initial_prompt", lambda _runtime: "legacy prompt")
    monkeypatch.setattr(runner, "_a2a_turn", turn)

    runner._run_a2a_legacy_smoke(runtime, FakeHarness(), FakeA2A())

    assert prompts[0] == "legacy prompt"
    assert "cn-hangzhou" in prompts[1]
    assert json.loads((runtime.paths.artifacts_dir / "waiting-sequence.json").read_text(encoding="utf-8")) == [
        "intent_parsing:ask_user_question",
        "confirm_and_select:candidate_selection",
    ]


def test_web_replacement_intent_does_not_prematurely_request_cancel(runner: ModuleType) -> None:
    for multimodal in (False, True):
        prompt = runner._web_replacement_intent_prompt(multimodal=multimodal)
        assert "新" in prompt or "改需求" in prompt
        assert "替换" in prompt or "不再创建" in prompt
        assert "取消" not in prompt
        assert "不部署" not in prompt


def test_web_candidate_selection_uses_long_action_timeout(runner: ModuleType) -> None:
    calls: list[tuple[str, str, str, object, float]] = []

    class FakeWeb:
        @staticmethod
        def _json_request(
            base_url: str,
            method: str,
            path: str,
            payload: object,
            *,
            timeout: float,
        ) -> dict[str, bool]:
            calls.append((base_url, method, path, payload, timeout))
            return {"accepted": True}

    result = runner._select_web_candidate(
        FakeWeb,
        "http://127.0.0.1:1",
        "model-session",
        timeout=123.0,
    )

    assert result == {"accepted": True}
    assert calls == [
        (
            "http://127.0.0.1:1",
            "POST",
            "/api/pipeline/candidates/select",
            {"sessionId": "model-session", "candidateIndex": 0, "parameterOverrides": {}},
            123.0,
        )
    ]


def test_web_session_uses_valid_unattended_permission_mode(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        paths=argparse.Namespace(workspace_dir=tmp_path / "workspace"),
        env={"IAC_CODE_PROVIDER": "dashscope", "IAC_CODE_MODEL": "test-model"},
    )

    payload = runner._web_session_create_payload(runtime)

    assert payload["permissionMode"] == "bypass_permissions"
    assert payload["pipelineName"] == runner.PIPELINE_NAME
    assert payload["mode"] == "pipeline"


def test_browser_dependency_preflight_reports_missing_node(runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: None)

    result = runner._run_browser_dependency_preflight(timeout=1.0)

    assert result == {"ok": False, "reason": "Node.js is unavailable"}


def test_browser_dependency_preflight_accepts_playwright_probe(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "/test/node")
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> argparse.Namespace:
        observed["command"] = command
        observed.update(kwargs)
        return argparse.Namespace(returncode=0, stdout="PLAYWRIGHT_CORE_OK\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_browser_dependency_preflight(timeout=7.0)

    assert result == {"ok": True, "reason": "PLAYWRIGHT_CORE_OK"}
    assert observed["command"][0] == "/test/node"
    assert observed["timeout"] == 7.0


def test_started_steps_accepts_repl_display_record_shape(runner: ModuleType) -> None:
    event = {"type": "step_started", "step_id": runner.NEW_STEPS[1], "payload": {"index": 2}}

    assert runner._started_steps([event]) == [(0, runner.NEW_STEPS[1])]


def test_ros_short_form_intrinsics_are_collected_as_templates(runner: ModuleType, tmp_path: Path) -> None:
    template = tmp_path / "template.yml"
    template.write_text(
        "ROSTemplateFormatVersion: '2015-09-01'\n"
        "Resources:\n"
        "  Vpc:\n"
        "    Type: ALIYUN::ECS::VPC\n"
        "Outputs:\n"
        "  VpcId:\n"
        "    Value: !GetAtt Vpc.VpcId\n",
        encoding="utf-8",
    )

    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load(template.read_text(encoding="utf-8"))
    assert runner._is_iac_template_file(template)


def _pipeline_check_runtime(runner: ModuleType, tmp_path: Path, profile: str) -> argparse.Namespace:
    return argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile=profile),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )


def test_deploy_order_uses_confirm_action_not_later_cancel(runner: ModuleType, tmp_path: Path) -> None:
    runtime = _pipeline_check_runtime(runner, tmp_path, "happy_multi")
    values = [
        {
            "eventType": "input_received",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {"kind": "deployment_confirmation", "action": "confirm"},
        },
        {"eventType": "tool_started", "data": {"toolName": "ros_deploy"}},
        {
            "eventType": "tool_result",
            "data": {"toolName": "ros_deploy", "result": '{"StackId": "stack-1"}'},
        },
        {
            "eventType": "input_received",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {"kind": "deployment_confirmation"},
        },
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["no deploy before confirmation"] is True


def test_old_step_check_uses_structured_ids_not_llm_text(runner: ModuleType, tmp_path: Path) -> None:
    runtime = _pipeline_check_runtime(runner, tmp_path, "backup_restore")
    values = [{"eventType": "status_update", "data": {"text": "以前叫 architecture_planning"}}]
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["old step ids absent"] is True

    values.append({"eventType": "step_started", "step": {"id": "architecture_planning"}})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["old step ids absent"] is False


def test_candidate_check_ignores_mentions_but_rejects_real_nested_transport_event(
    runner: ModuleType, tmp_path: Path
) -> None:
    runtime = _pipeline_check_runtime(runner, tmp_path, "rollback_step3")
    values = [{"eventType": "tool_result", "data": {"toolName": "bash", "result": {
        "documentation": "candidate_step_started", "example": {"eventType": "candidate_step_started"},
    }}}]
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["candidate sub-pipeline absent"] is True
    values.append({"metadata": {"iac_code": {"pipeline": {"eventType": "candidate_step_started"}}}})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["candidate sub-pipeline absent"] is False


@pytest.mark.parametrize('failure', ['', 'new_pipeline', 'new_step', 'missing_identity', 'empty'])
def test_legacy_identity_uses_native_pipeline_and_steps_not_quoted_names(runner, tmp_path, failure):
    runtime = _pipeline_check_runtime(runner, tmp_path, 'legacy_smoke')
    runtime.spec.surface = runner.Surface.LEGACY
    runtime.env['IAC_CODE_PIPELINE_NAME'] = 'selling'
    values = [
        {'metadata': {'iac_code': {'pipeline': {
            'eventType': 'step_started', 'pipelineName': 'selling', 'step': {'id': 'intent_parsing'},
        }}}},
        {'eventType': 'tool_result', 'pipelineName': 'selling', 'data': {
            'toolName': 'read_file', 'result': {
                'documentation': 'The selling_solution_first flow is separate.',
                'example': {'pipelineName': 'selling_solution_first', 'eventType': 'step_started',
                            'step': {'id': 'solution_planning_and_selection'}},
            },
        }},
    ]
    if failure == 'new_pipeline':
        values[0]['metadata']['iac_code']['pipeline']['pipelineName'] = 'selling_solution_first'
    elif failure == 'new_step':
        values[0]['metadata']['iac_code']['pipeline']['step']['id'] = 'solution_planning_and_selection'
        values = values[:1]
    elif failure == 'missing_identity':
        values = [{'eventType': 'step_started', 'step': {'id': 'intent_parsing'}}]
    elif failure == 'empty':
        values = []
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks['legacy pipeline not rewritten'] is (not failure)


def test_tool_sequence_ignores_named_examples_but_preserves_actual_deploy_order(
    runner: ModuleType, tmp_path: Path
) -> None:
    runtime = _pipeline_check_runtime(runner, tmp_path, "image_asks")
    values = [{"eventType": "tool_result", "data": {"toolName": "bash", "result": {
        "tools": [{"name": "ros_deploy"}, {"toolName": "ros_deploy"}],
    }}}]
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["no deploy before confirmation"] is True
    values.append({"metadata": {"iac_code": {"pipeline": {
        "eventType": "tool_started", "data": {"toolName": "ros_deploy"},
    }}}})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["no deploy before confirmation"] is False


@pytest.mark.parametrize('mention', ['PreviewStack', 'GetTemplateEstimateCost', '"toolName": "write"'])
def test_step1_materialization_gate_ignores_documentation_in_real_tool_output(runner, tmp_path, mention):
    runtime = _pipeline_check_runtime(runner, tmp_path, 'image_handoff')
    values = [
        {'eventType': 'step_started', 'step': {'id': runner.NEW_STEPS[0]}},
        {'eventType': 'tool_result', 'data': {'toolName': 'read_file', 'result': mention}},
        {'eventType': 'step_started', 'step': {'id': runner.NEW_STEPS[1]}},
        {'eventType': 'tool_started', 'data': {'toolName': 'ros_preview_template'}},
    ]
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks['Step 1 has no materialization or exact quote'] is True


@pytest.mark.parametrize('tool,inputs', [
    ('ros_preview_template', {}), ('ros_estimate_template_cost', {}), ('write_file', {}),
    ('aliyun_api', {'action': 'PreviewStack'}), ('aliyun_api', {'action': 'GetTemplateEstimateCost'}),
])
@pytest.mark.parametrize('rollback', [False, True])
def test_step1_materialization_gate_rejects_actual_calls_in_each_planning_attempt(
    runner, tmp_path, tool, inputs, rollback,
):
    runtime = _pipeline_check_runtime(runner, tmp_path, 'image_handoff')
    values = [{'eventType': 'step_started', 'step': {'id': runner.NEW_STEPS[0]}}]
    if rollback:
        values += [{'eventType': 'step_started', 'step': {'id': runner.NEW_STEPS[1]}},
                   {'eventType': 'step_started', 'step': {'id': runner.NEW_STEPS[0]}}]
    values.append({'eventType': 'tool_started', 'data': {'toolName': tool, 'input': inputs}})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks['Step 1 has no materialization or exact quote'] is False


def test_safe_cancel_requires_that_no_deployment_was_attempted(runner: ModuleType, tmp_path: Path) -> None:
    # A02 cancels instead of confirming, so ros_deploy must never be reached. Safe mode does not
    # restrict step tools, so an attempted deployment there would be a real cloud write.
    runtime = _pipeline_check_runtime(runner, tmp_path, "safe_cancel")
    canceled = [
        {
            "eventType": "input_received",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {"kind": "deployment_confirmation"},
        },
    ]

    runner._common_pipeline_checks(runtime, canceled)

    assert runtime.checks["cancel kept the deployment unattempted"] is True
    assert runtime.checks["safe mode and cancel made no cloud write"] is True

    attempted = _pipeline_check_runtime(runner, tmp_path, "safe_cancel")
    runner._common_pipeline_checks(
        attempted,
        [*canceled, {"eventType": "tool_started", "data": {"toolName": "ros_deploy"}}],
    )

    assert attempted.checks["cancel kept the deployment unattempted"] is False


def test_deploy_order_accepts_repl_display_confirmation_shape(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.REPL, profile="happy_single"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"kind": "deployment_confirmation", "action": "confirm"},
        },
        {"type": "tool_used", "step_id": runner.NEW_STEPS[2], "payload": {"name": "ros_deploy"}},
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["no deploy before confirmation"] is True


def test_deploy_order_accepts_repl_free_text_only_when_it_enters_step3(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.REPL, profile="natural_adjust"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"kind": "deployment_confirmation", "structured": False, "selected_value": "调整参数"},
        },
        {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {}},
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"kind": "deployment_confirmation", "structured": False, "selected_value": "确认部署"},
        },
        {"type": "step_started", "step_id": runner.NEW_STEPS[2]},
        {"type": "tool_used", "step_id": runner.NEW_STEPS[2], "payload": {"name": "ros_deploy"}},
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["no deploy before confirmation"] is True

    runtime.checks = {}
    runner._common_pipeline_checks(runtime, [values[0], values[-1]])
    assert runtime.checks["no deploy before confirmation"] is False


def test_confirmation_acceptance_uses_structured_free_quote(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile="step2_parameter"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "eventType": "input_required",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {
                "kind": "deployment_confirmation",
                "solution_summary": "在已有 VPC 下创建一个 VSwitch。",
                "cost": {
                    "quote_status": "succeeded",
                    "monthly_estimate": "¥0/月",
                    "resources": [],
                },
            },
        }
    ]

    runner._common_pipeline_checks(runtime, values)

    assert runtime.checks["confirmation includes current solution and quote"] is True
    assert runtime.checks["A2A waiting input was exercised"] is True


def test_successful_quote_must_be_projected_as_succeeded(runner: ModuleType, tmp_path: Path) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile="step2_parameter"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    values = [
        {
            "eventType": "tool_result",
            "data": {"toolName": "ros_estimate_template_cost", "isError": False, "result": {"Resources": []}},
        },
        {
            "eventType": "input_required",
            "step": {"id": runner.NEW_STEPS[1]},
            "data": {
                "kind": "deployment_confirmation",
                "solution_summary": "create a network",
                "cost": {
                    "quote_status": "unavailable",
                    "monthly_estimate": "询价不可用",
                    "resources": [],
                },
            },
        },
    ]

    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["successful ROS quote projected into confirmation"] is False

    values[1]["data"]["cost"].update({"quote_status": "succeeded", "monthly_estimate": "¥0/月"})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks["successful ROS quote projected into confirmation"] is True


def _nonzero_ros_quote_without_period():
    return {'Resources': {'NetworkAddress': {'Success': True, 'Type': 'ALIYUN::VPC::EIP',
                                           'Result': {'Order': {'OriginalAmount': '0.8', 'TradeAmount': '0.8',
                                                                'Currency': 'CNY'}}}}}


def test_quote_without_period_reproduces_old_false_failure_without_inventing_a_monthly_price(
    runner, tmp_path, monkeypatch,
):
    from iac_code.pipeline.selling_solution_first.hooks.materialize_selected_candidate import _quote_projection

    runtime = _pipeline_check_runtime(runner, tmp_path, 'step2_parameter')
    quote = _nonzero_ros_quote_without_period()
    cost = _quote_projection({'result': quote, 'is_error': False})
    assert cost['quote_status'] == 'unavailable' and cost['resources'] == []
    values = [
        {'eventType': 'tool_result', 'data': {'toolName': 'ros_estimate_template_cost',
                                            'isError': False, 'result': quote}},
        {'eventType': 'input_required', 'step': {'id': runner.NEW_STEPS[1]}, 'data': {
            'kind': 'deployment_confirmation', 'solution_summary': 'network', 'cost': cost}},
    ]
    with monkeypatch.context() as old:
        old.setattr(runner, '_quote_without_billing_period', lambda value: False)
        runner._common_pipeline_checks(runtime, values)
        assert runtime.checks['successful ROS quote projected into confirmation'] is False
    runtime.checks = {}
    runner._common_pipeline_checks(runtime, values)
    assert 'successful ROS quote projected into confirmation' not in runtime.checks
    assert runtime.checks['ROS quote without a billing period is reported unavailable'] is True
    assert runtime.checks['confirmation includes current solution and quote'] is True


@pytest.mark.parametrize('change', [
    {'quote_status': 'succeeded', 'monthly_estimate': '¥0/月'},
    {'monthly_estimate': '¥576/月'}, {'resources': [{'cost': '¥0/月'}]},
    {'error': ''}, {'monthly_estimate': ''},
    {'monthly_estimate': '免费'}, {'monthly_estimate': 'Free'}, {'monthly_estimate': '可以部署'},
])
def test_unpriced_quote_cannot_pass_with_a_fabricated_monthly_price_or_missing_reason(runner, tmp_path, change):
    runtime = _pipeline_check_runtime(runner, tmp_path, 'step2_parameter')
    cost = {'quote_status': 'unavailable', 'monthly_estimate': '询价不可用', 'resources': [], 'error': 'Missing period'}
    cost.update(change)
    values = [
        {'eventType': 'tool_result', 'data': {'toolName': 'ros_estimate_template_cost',
                                            'isError': False, 'result': _nonzero_ros_quote_without_period()}},
        {'eventType': 'input_required', 'step': {'id': runner.NEW_STEPS[1]}, 'data': {
            'kind': 'deployment_confirmation', 'solution_summary': 'network', 'cost': cost}},
    ]
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks['ROS quote without a billing period is reported unavailable'] is False


def test_unpriced_quote_checks_every_confirmation_when_no_price_is_usable(runner, tmp_path):
    runtime = _pipeline_check_runtime(runner, tmp_path, 'step2_parameter')
    values = [{'eventType': 'tool_result', 'data': {'toolName': 'ros_estimate_template_cost',
               'isError': False, 'result': _nonzero_ros_quote_without_period()}}]
    for status, label in [('unavailable', '询价不可用'), ('succeeded', '¥0/月')]:
        values.append({'eventType': 'input_required', 'step': {'id': runner.NEW_STEPS[1]}, 'data': {
            'kind': 'deployment_confirmation', 'solution_summary': 'network', 'cost': {
                'quote_status': status, 'monthly_estimate': label, 'resources': [], 'error': 'Missing period'}}})
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks['ROS quote without a billing period is reported unavailable'] is False


@pytest.mark.parametrize('change', [
    {'Currency': 'USD'}, {'TradeAmount': True}, {'TradeAmount': '-1'}, {'TradeAmount': 'NaN'},
    {'PriceUnit': '/Hour'}, {'PeriodUnit': 'Month'}, {'Period': 1},
])
def test_unpriced_quote_exception_rejects_invalid_amounts_and_possible_period_fields(runner, change):
    quote = _nonzero_ros_quote_without_period()
    quote['Resources']['NetworkAddress']['Result']['Order'].update(change)
    assert runner._quote_without_billing_period(quote) is False


def test_native_period_or_failed_resource_keeps_the_original_successful_quote_gate(runner, tmp_path):
    quote = _nonzero_ros_quote_without_period()
    quote['Resources']['NetworkAddress']['Result']['OrderSupplement'] = {'PriceUnit': '/Hour'}
    assert runner._quote_without_billing_period(quote) is False
    runtime = _pipeline_check_runtime(runner, tmp_path, 'step2_parameter')
    values = [
        {'eventType': 'tool_result', 'data': {'toolName': 'ros_estimate_template_cost',
                                            'isError': False, 'result': quote}},
        {'eventType': 'input_required', 'step': {'id': runner.NEW_STEPS[1]}, 'data': {
            'kind': 'deployment_confirmation', 'solution_summary': 'network',
            'cost': {'quote_status': 'unavailable', 'monthly_estimate': '询价不可用', 'resources': []}}},
    ]
    runner._common_pipeline_checks(runtime, values)
    assert runtime.checks['successful ROS quote projected into confirmation'] is False
    quote['Resources']['NetworkAddress']['Success'] = False
    assert runner._quote_without_billing_period(quote) is False


def test_unpriced_quote_recognition_accepts_only_the_native_ros_preflight_suffix(runner):
    response = json.dumps(_nonzero_ros_quote_without_period())
    assert runner._quote_without_billing_period(response + '\n---\nROS preflight\nvalidation notes') is True
    assert runner._quote_without_billing_period(response + '\nuntrusted trailing text') is False


def test_quote_example_in_tool_output_cannot_trigger_successful_quote_acceptance(runner, tmp_path):
    runtime = SimpleNamespace(
        spec=SimpleNamespace(surface=runner.Surface.A2A, profile='step2_parameter'),
        env={'IAC_CODE_PIPELINE_NAME': runner.PIPELINE_NAME},
        paths=SimpleNamespace(run_dir=tmp_path, artifacts_dir=tmp_path / 'artifacts'),
        owned_stack_names=set(), checks={}, diagnostics={},
    )
    values = [
        {'eventType': 'tool_result', 'data': {'toolName': 'read', 'isError': False, 'result': {
            'example': {'eventType': 'tool_result', 'data': {
                'toolName': 'ros_estimate_template_cost', 'isError': False, 'result': {'cost': 1}}}}}},
        {'eventType': 'input_required', 'step': {'id': runner.NEW_STEPS[1]}, 'data': {
            'kind': 'deployment_confirmation', 'solution_summary': 'network',
            'cost': {'quote_status': 'unavailable', 'monthly_estimate': '询价不可用', 'resources': []}}},
    ]
    runner._common_pipeline_checks(runtime, values)
    assert 'successful ROS quote projected into confirmation' not in runtime.checks
    assert runtime.diagnostics['quote_tool_result_count'] == 0


def test_fault_checkpoint_answers_new_selection_but_kills_only_at_real_target_event(runner, monkeypatch, tmp_path):
    pending_summary = SimpleNamespace(name='waiting', last_status_state='TASK_STATE_INPUT_REQUIRED',
                                      last_input_required_step_id=runner.NEW_STEPS[0])
    actions = []

    class Stream:
        summary = pending_summary

        def wait_for(self, predicate, **kwargs):
            raise RuntimeError('stream ended at real candidate selection')

        def join(self, **kwargs):
            actions.append('join')

    class ValidatedStream(Stream):
        def wait_for(self, predicate, **kwargs):
            assert not predicate({'tool': 'unrelated'}, None)
            assert predicate({'tool': 'validated'}, None)
            actions.append('validated')

    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=10), paths=SimpleNamespace(run_dir=tmp_path),
                              diagnostics={}, checks={}, spec=SimpleNamespace(profile='fault_checkpoints'),
                              event=lambda *_a, **_k: actions.append('restart-event'))
    harness = SimpleNamespace(kill9=lambda: actions.append('kill'), start_server=lambda: actions.append('start'))

    def start_stream(**kwargs):
        assert kwargs['prompt'] == runner._candidate_payload(0)
        assert 'kill' not in actions
        actions.append('answer')
        return ValidatedStream()

    harness.start_stream = start_stream
    monkeypatch.setattr(runner, '_legacy_a2a_module', lambda: SimpleNamespace())
    monkeypatch.setattr(runner, '_pending_kind', lambda *_: 'candidate_selection')
    monkeypatch.setattr(runner, '_latest_a2a_pending_question', lambda *_: {'kind': 'candidate_selection'})
    plan = SimpleNamespace(candidate_answers=[], image_kinds=set())
    runner._kill_restart_at(runtime, harness, Stream(), lambda e, _: e['tool'] == 'validated',
                            'template-written-validated', plan=plan)
    assert actions.index('validated') < actions.index('kill')
    assert actions.count('kill') == 1
    assert runtime.checks['template-written-validated event verified'] is True
    assert runtime.diagnostics['fault_pending_input_count'] == 1


@pytest.mark.parametrize('state,transport_error', [
    ('TASK_STATE_FAILED', None), ('TASK_STATE_INPUT_REQUIRED', RuntimeError('transport failed')),
])
def test_fault_checkpoint_does_not_retry_failed_product_task(runner, tmp_path, state, transport_error):
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=10), paths=SimpleNamespace(run_dir=tmp_path),
                              diagnostics={}, checks={})

    def wait_for(*_a, **_kw):
        raise RuntimeError('product failed')

    stream = SimpleNamespace(wait_for=wait_for, summary=SimpleNamespace(last_status_state=state),
                             exception=transport_error)
    harness = SimpleNamespace(kill9=lambda: pytest.fail('must not kill to rescue a failed product task'))
    with pytest.raises(RuntimeError, match='product failed'):
        runner._kill_restart_at(runtime, harness, stream, lambda *_: False, 'quote-saved', plan=SimpleNamespace())
    assert runtime.diagnostics['fault_failed_checkpoint'] == 'quote-saved'


def test_common_checks_ignore_handled_tool_traceback_but_reject_terminal_traceback(
    runner: ModuleType, tmp_path: Path
) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(surface=runner.Surface.A2A, profile="step2_parameter"),
        env={"IAC_CODE_PIPELINE_NAME": runner.PIPELINE_NAME},
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        owned_stack_names=set(),
        checks={},
    )
    handled_tool_error = {
        "result": {
            "statusUpdate": {
                "metadata": {
                    "iac_code": {
                        "pipeline": {
                            "eventType": "tool_result",
                            "data": {
                                "isError": True,
                                "result": "STDERR:\nTraceback (most recent call last):\nValueError: bad input",
                            },
                        }
                    }
                }
            }
        }
    }

    runner._common_pipeline_checks(runtime, [handled_tool_error])
    assert runtime.checks["no unhandled terminal error"] is True
    assert "A2A waiting input was exercised" not in runtime.checks
    assert "confirmation includes current solution and quote" not in runtime.checks

    runner._common_pipeline_checks(
        runtime,
        [{"transcript": "Bash output:\nTraceback (most recent call last):\nModuleNotFoundError: optional tool"}],
    )
    assert runtime.checks["no unhandled terminal error"] is True

    runner._common_pipeline_checks(runtime, [{"message": "cancel before deployment_confirmation"}])
    assert "confirmation includes current solution and quote" not in runtime.checks

    runner._common_pipeline_checks(runtime, [{"error": "Traceback (most recent call last):\nRuntimeError: boom"}])
    assert runtime.checks["no unhandled terminal error"] is False


def test_repl_artifacts_reject_child_exit_before_runner_teardown(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".cloud-credentials.yml").write_text(
        "access_key_secret: cloud-secret-value\n", encoding="utf-8"
    )
    runtime = argparse.Namespace(
        env={},
        paths=argparse.Namespace(run_dir=tmp_path, config_dir=config_dir),
        checks={},
    )
    pty = argparse.Namespace(
        transcript="handled tool output cloud-secret-value",
        events=[
            {
                "type": "terminate",
                "detail": "cloud-secret-value",
                "force": False,
                "aliveBeforeTerminate": False,
                "exitStatus": 1,
                "signalStatus": None,
            }
        ],
    )
    repl = argparse.Namespace(
        _redact_sensitive_text=lambda text, _env: text,
        _normalize_transcript=lambda text: text,
    )
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: [])
    monkeypatch.setattr(runner, "_common_pipeline_checks", lambda *_args: None)

    runner._write_repl_artifacts(runtime, pty, repl)

    assert runtime.checks["REPL stayed alive until teardown"] is False
    assert runtime.checks["REPL has no terminal exception"] is False
    recorded = json.loads((tmp_path / "repl-events.jsonl").read_text(encoding="utf-8"))
    assert recorded["exitStatus"] == 1
    assert "cloud-secret-value" not in (tmp_path / "transcript.raw.log").read_text(encoding="utf-8")
    assert "cloud-secret-value" not in (tmp_path / "repl-events.jsonl").read_text(encoding="utf-8")


def test_first_pending_resource_option_id_ignores_control_actions(runner: ModuleType) -> None:
    a2a = argparse.Namespace(
        _extract_pipeline_envelopes=lambda event: event["envelopes"],
    )
    event = {
        "envelopes": [
            {
                "eventType": "input_required",
                "data": {
                    "kind": "ask_user_question",
                    "options": [
                        {"label": "existing VPC", "id": "vpc-test123"},
                        {"label": "other", "id": "vpc-test456"},
                    ],
                },
            }
        ]
    }

    assert runner._first_pending_resource_option_id(a2a, event) == "vpc-test123"
    control_event = {
        "envelopes": [
            {
                "eventType": "input_required",
                "data": {"options": [{"label": "open console", "id": "open_console"}]},
            }
        ]
    }
    assert runner._first_pending_resource_option_id(a2a, control_event) == ""
    assert runner._first_pending_resource_option_id(a2a, {"envelopes": []}) == ""


def test_input_received_kind_and_step_matches_candidate_selection(runner: ModuleType) -> None:
    a2a = argparse.Namespace(_extract_pipeline_envelopes=lambda event: event["envelopes"])
    predicate = runner._input_received_kind_and_step(
        a2a,
        runner.NEW_STEPS[0],
        "candidate_selection",
    )
    matching = {
        "envelopes": [
            {
                "eventType": "input_received",
                "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection", "selectedIndex": 0},
            }
        ]
    }

    assert predicate(matching, None) is True
    assert predicate({"envelopes": [{"eventType": "input_required", "data": {}}]}, None) is False


def test_successful_tool_result_matches_solution_first_quote_tool(runner: ModuleType) -> None:
    a2a = argparse.Namespace(_extract_pipeline_envelopes=lambda event: event["envelopes"])
    predicate = runner._successful_tool_result(a2a, "ros_estimate_template_cost")

    assert predicate(
        {
            "envelopes": [
                {
                    "eventType": "tool_result",
                    "data": {"toolName": "ros_estimate_template_cost", "isError": False, "result": {"cost": 1}},
                }
            ]
        },
        None,
    )
    assert not predicate(
        {
            "envelopes": [
                {
                    "eventType": "tool_result",
                    "data": {"toolName": "ros_estimate_template_cost", "isError": True},
                }
            ]
        },
        None,
    )


def test_event_files_follow_request_order_for_recovery_streams(runner: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "fault-after-quote.events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "fault-after-snapshot.events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "fault-final.events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "requests.jsonl").write_text(
        "\n".join(json.dumps({"name": name}) for name in ("fault-after-snapshot", "fault-after-quote", "fault-final"))
        + "\n",
        encoding="utf-8",
    )

    assert [path.name for path in runner._event_files(tmp_path)] == [
        "fault-after-snapshot.events.jsonl",
        "fault-after-quote.events.jsonl",
        "fault-final.events.jsonl",
    ]


def test_runtime_paths_are_isolated(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "real-config"
    source.mkdir()
    paths = runner.RuntimePaths.create(tmp_path / "run", source)
    assert paths.config_dir != paths.backup_dir != paths.workspace_dir
    assert all(
        runner.is_relative_to(path, paths.run_dir) for path in (paths.config_dir, paths.backup_dir, paths.workspace_dir)
    )
    with pytest.raises(ValueError, match="credential source"):
        runner.RuntimePaths.create(tmp_path, tmp_path / "config")


def test_create_runtime_isolates_env_and_cloud_identity(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text("fake: value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "a2a-step1-clarify",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )
    runtime = runner.create_runtime(runner.SCENARIO_BY_NAME["a2a-step1-clarify"], args, runner.RunnerServices(), {})
    assert runtime.env["IAC_CODE_PIPELINE_NAME"] == "selling_solution_first"
    assert runtime.env["IAC_CODE_CONFIG_DIR"] == str(runtime.paths.config_dir)
    assert runtime.env["IAC_CODE_CONFIG_BACKUP_DIR"] == str(runtime.paths.backup_dir)
    assert runtime.stack_name.startswith("iac-e2e-ssf-a2a-step1-clarify-")
    assert runtime.cidr.startswith("10.250.")
    assert runner.is_relative_to(runtime.paths.config_dir, runtime.paths.run_dir)
    assert runner.is_relative_to(runtime.paths.backup_dir, runtime.paths.run_dir)
    assert runner.is_relative_to(runtime.paths.workspace_dir, runtime.paths.run_dir)


def test_multimodal_runtime_default_is_not_overridden_by_inherited_text_model(
    runner: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text("fake: value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "repl-multimodal-lifecycle",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )

    runtime = runner.create_runtime(
        runner.SCENARIO_BY_NAME["repl-multimodal-lifecycle"],
        args,
        runner.RunnerServices(),
        {"provider": "dashscope", "model": runner.DEFAULT_TEXT_MODEL},
    )

    assert runtime.env["IAC_CODE_MODEL"] == runner.DEFAULT_MULTIMODAL_MODEL


def test_explicit_model_overrides_multimodal_runtime_default(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text("fake: value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "repl-multimodal-lifecycle",
            "--model",
            "explicit-vision-model",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )

    runtime = runner.create_runtime(
        runner.SCENARIO_BY_NAME["repl-multimodal-lifecycle"],
        args,
        runner.RunnerServices(),
        {"provider": "dashscope", "model": runner.DEFAULT_TEXT_MODEL},
    )

    assert runtime.env["IAC_CODE_MODEL"] == "explicit-vision-model"


def test_runtime_rejects_case_directory_inside_real_config(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="credential source"):
        runner.RuntimePaths.create(source / "case", source)


def test_port_and_cidr_allocators_are_thread_safe(runner: ModuleType) -> None:
    ports = runner.PortAllocator()
    cidrs = runner.CidrAllocator(["10.250.10.0/24"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        allocated_ports = list(pool.map(lambda _: ports.reserve(), range(40)))
        allocated_cidrs = list(pool.map(lambda _: cidrs.reserve(), range(40)))
    assert len(set(allocated_ports)) == 40
    assert len(set(allocated_cidrs)) == 40
    assert "10.250.10.0/24" not in allocated_cidrs
    vpc_allocator = runner.CidrAllocator(["192.168.0.0/24"], "192.168.0.0/16")
    assert vpc_allocator.reserve() == "192.168.1.0/24"


def test_named_resource_lock_only_serializes_same_name(runner: ModuleType) -> None:
    manager = runner.ResourceLockManager()
    active = 0
    maximum = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal active, maximum
        with manager.acquire("shared"):
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with guard:
                active -= 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: worker(), range(8)))
    assert maximum == 1


def _execution_args(runner: ModuleType, tmp_path: Path, *, concurrency: int = 3) -> argparse.Namespace:
    return argparse.Namespace(
        concurrency=concurrency,
        fail_fast=False,
        run_root=str(tmp_path),
        run_dir="",
    )


def test_worker_pool_honors_concurrency_and_returns_registry_order(runner: ModuleType, tmp_path: Path) -> None:
    selected = list(runner.SCENARIOS[:8])
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_run(spec, _args, _services, _defaults, _root):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return runner.ScenarioResult(
            spec.case_id,
            spec.name,
            spec.surface.value,
            "passed",
            "start",
            "end",
            0.02,
            str(tmp_path / spec.name),
            {"fake": True},
            [],
            "completed",
        )

    results = runner.execute_selected(
        selected,
        _execution_args(runner, tmp_path),
        runner.RunnerServices(),
        {},
        tmp_path,
        run_one=fake_run,
    )
    assert maximum == 3
    assert [item.scenario for item in results] == [item.name for item in selected]
    assert all(item.passed for item in results)


def test_fail_fast_marks_unscheduled_cases_and_exit_codes(runner: ModuleType, tmp_path: Path) -> None:
    selected = list(runner.SCENARIOS[:3])
    args = _execution_args(runner, tmp_path, concurrency=1)
    args.fail_fast = True

    def fake_run(spec, _args, _services, _defaults, _root):
        return runner.ScenarioResult(
            spec.case_id,
            spec.name,
            spec.surface.value,
            "failed",
            "start",
            "end",
            0.0,
            "",
            {"fake": False},
            [],
            "completed",
        )

    results = runner.execute_selected(
        selected,
        args,
        runner.RunnerServices(),
        {},
        tmp_path,
        run_one=fake_run,
    )
    assert [item.status for item in results] == ["failed", "not-started", "not-started"]
    assert runner.suite_exit_code(results, credential_unchanged=True, interrupted=False) == 1
    assert runner.suite_exit_code([], credential_unchanged=False, interrupted=False) == 1
    assert runner.suite_exit_code([], credential_unchanged=True, interrupted=True) == 130
    assert runner.suite_exit_code([], credential_unchanged=True, interrupted=False) == 0


def test_terminate_processes_stops_registered_child(runner: ModuleType, tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("signal behavior is covered by Windows CI integration tests")
    process = __import__("subprocess").Popen([__import__("sys").executable, "-c", "import time; time.sleep(60)"])
    runtime = object.__new__(runner.ScenarioRuntime)
    runtime.processes = [process]
    assert runtime.terminate_processes()
    assert process.poll() is not None


def test_a2a_helper_server_process_is_registered_for_suite_interrupt(runner: ModuleType) -> None:
    process = __import__("subprocess").Popen([__import__("sys").executable, "-c", "import time; time.sleep(60)"])
    runtime = object.__new__(runner.ScenarioRuntime)
    runtime.processes = []

    class Harness:
        server = None

        def start_server(self) -> None:
            self.server = argparse.Namespace(process=process)

    harness = Harness()
    runner._track_a2a_server_processes(runtime, harness)
    try:
        harness.start_server()
        harness.start_server()
        assert runtime.processes == [process]
    finally:
        runtime.terminate_processes()


def test_terminate_active_processes_attempts_every_runtime(runner: ModuleType) -> None:
    calls: list[str] = []

    class Runtime:
        def __init__(self, name: str, clean: bool) -> None:
            self.name = name
            self.clean = clean

        def terminate_processes(self) -> bool:
            calls.append(self.name)
            return self.clean

    services = runner.RunnerServices()
    services.active_runtimes = {"first": Runtime("first", False), "second": Runtime("second", True)}
    assert not services.terminate_active_processes()
    assert calls == ["first", "second"]


def test_desktop_result_requires_the_full_native_contract(runner: ModuleType) -> None:
    result = {
        "pipelineName": runner.PIPELINE_NAME,
        "steps": list(runner.NEW_STEPS),
        **{name: True for name in runner.DESKTOP_RESULT_CHECKS},
        "cloudWriteObserved": False,
        "packageResources": {
            "yaml": True,
            "prompts": True,
            "skills": True,
            "hooks": True,
            "tools": True,
            "references": True,
        },
    }
    assert all(runner.validate_desktop_result(result).values())
    result["confirmationWaitingRestartRecovered"] = False
    assert not all(runner.validate_desktop_result(result).values())


def test_desktop_source_resource_audit_follows_linked_reference_directory(runner: ModuleType, tmp_path: Path) -> None:
    source_root = tmp_path / "pipeline"
    shared_references = tmp_path / "shared-references"
    linked_references = source_root / "skills" / "materialize" / "references"
    shared_references.mkdir()
    (shared_references / "ros-template.md").write_text("reference", encoding="utf-8")
    linked_references.parent.mkdir(parents=True)
    linked_references.symlink_to(shared_references, target_is_directory=True)

    audit = runner.audit_desktop_source_resources(
        source_root,
        ("skills/materialize/references/ros-template.md", "pipeline.yaml"),
    )

    assert audit["sourceResourcesPresent"] == ["skills/materialize/references/ros-template.md"]
    assert audit["missingSourceResources"] == ["pipeline.yaml"]
    assert audit["allPresent"] is False


def test_case_artifact_credential_audit_ignores_config_but_detects_log_leak(runner: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("dashscope: unit-secret-value\n", encoding="utf-8")
    (source / ".cloud-credentials.yml").write_text("access_key_secret: cloud-secret-value\n", encoding="utf-8")
    args = runner.parse_args(
        [
            "--scenario",
            "a2a-step1-clarify",
            "--concurrency",
            "1",
            "--run-root",
            str(tmp_path / "runs"),
            "--credential-source-dir",
            str(source),
        ]
    )
    runtime = runner.create_runtime(runner.SCENARIO_BY_NAME["a2a-step1-clarify"], args, runner.RunnerServices(), {})
    assert runner.credential_values_absent_from_artifacts(runtime)
    preflight_config = runtime.paths.run_dir / ".preflight" / "config"
    preflight_config.mkdir(parents=True)
    (preflight_config / ".credentials.yml").write_text("api_key: unit-secret-value\n", encoding="utf-8")
    assert runner.credential_values_absent_from_artifacts(runtime)
    credential_source = runtime.paths.run_dir / "credential-source"
    credential_source.mkdir()
    (credential_source / ".cloud-credentials.yml").write_text(
        "access_key_secret: cloud-secret-value\n", encoding="utf-8"
    )
    assert runner.credential_values_absent_from_artifacts(runtime)
    (runtime.paths.logs_dir / "leak.log").write_text("unit-secret-value", encoding="utf-8")
    assert not runner.credential_values_absent_from_artifacts(runtime)
    assert runtime.notes[-1] == "credential audit: source=llm; location=logs; suffix=log"
    assert runtime.diagnostics['credential_audit_credential_kind'] == 'api_key'
    assert "unit-secret-value" not in runtime.notes[-1]
    (runtime.paths.logs_dir / "leak.log").unlink()
    (runtime.paths.run_dir / "captured.task-get.json").write_text(
        '{"content": "cloud-secret-value"}', encoding="utf-8"
    )
    assert not runner.credential_values_absent_from_artifacts(runtime)
    assert runtime.notes[-1] == "credential audit: source=cloud; location=other; suffix=json; artifact=a2a_task"
    assert "cloud-secret-value" not in runtime.notes[-1]
    assert runtime.diagnostics['credential_audit_source'] == 'cloud'
    assert runtime.diagnostics['credential_audit_credential_kind'] == 'access_key_secret'
    assert runtime.diagnostics['credential_audit_origins'] == ['other']
    assert runtime.diagnostics['credential_audit_fields'] == ['content']
    assert re.fullmatch('[0-9a-f]{64}', runtime.diagnostics['credential_audit_file_hash'])
    assert 'cloud-secret-value' not in json.dumps(runtime.diagnostics)
    (runtime.paths.run_dir / 'captured.task-get.json').unlink()
    (runtime.paths.run_dir / 'final-pipeline-state.pipeline-state.json').write_text(
        '{"context": {"private-secret-key": {"value": "cloud-secret-value"}}}', encoding='utf-8',
    )
    assert not runner.credential_values_absent_from_artifacts(runtime)
    assert 'artifact=pipeline_state' in runtime.notes[-1]
    assert runtime.diagnostics['credential_audit_fields'] == ['context', 'value']
    assert 'private-secret-key' not in json.dumps(runtime.diagnostics)


def test_reused_web_browser_helper_accepts_optional_dom_artifacts(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = runner._web_module()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    web._verify_browser(
        base_url="http://127.0.0.1:1",
        session_id="session-1",
        expected_text="方案",
        screenshot=tmp_path / "screen.png",
        dom_snapshot=tmp_path / "dom.txt",
        audit=tmp_path / "audit.json",
        require_quote=True,
        expand_pipeline_history=True,
    )
    command = captured["command"]
    assert isinstance(command, list)
    assert "--domSnapshot" in command
    assert "--audit" in command
    assert command[-4:] == [
        "--requireQuote",
        "true",
        "--expandPipelineHistory",
        "true",
    ]


def test_repl_selection_waits_for_durable_display_event_occurrence(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "candidate_selection_ready", "payload": {"round": 1}}),
                json.dumps({"type": "candidate_selection_ready", "payload": {"round": 2}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
        repl_candidate_wait_count=1,
    )
    pty = argparse.Namespace(events=[])

    runner._repl_wait_selection(pty, runtime)

    assert runtime.repl_candidate_wait_count == 2
    assert pty.events == [
        {
            "type": "display-event",
            "description": "selling_solution_first candidate selection",
            "event_type": "candidate_selection_ready",
            "occurrence": 2,
            "path": str(display_path),
            "at": pty.events[0]["at"],
        }
    ]


def test_repl_selection_after_restart_waits_for_live_controls(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path),
        args=argparse.Namespace(stream_timeout=1.0),
        repl_candidate_wait_count=0,
    )
    pty = argparse.Namespace(events=[], transcript="Enter to confirm", drain_output=lambda: calls.append("drain"))

    def wait_for_display(*_args: object, **kwargs: object) -> tuple[dict[str, str], Path]:
        calls.append(("journal", callable(kwargs["drain_output"])))
        return {"type": "candidate_selection_ready"}, tmp_path

    monkeypatch.setattr(
        runner,
        "_wait_repl_display_event",
        wait_for_display,
    )
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(
            _normalize_transcript=lambda value: value,
            CANDIDATE_SELECTION_READY_PATTERNS=("Enter to confirm",),
        ),
    )
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())

    runner._repl_wait_selection(pty, runtime, after_restart=True, terminal_offset=0)

    assert calls == [("journal", True), "drain"]
    assert runtime.repl_candidate_wait_count == 1


def test_repl_selection_timeout_keeps_occurrence_for_retry(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0), repl_candidate_wait_count=1)
    pty = argparse.Namespace(events=[], transcript="")

    def wait_for_display(*_args, **kwargs):
        assert kwargs["occurrence"] == 2
        assert kwargs["timeout"] == 4.0
        raise TimeoutError("stalled")

    monkeypatch.setattr(runner, "_wait_repl_display_event", wait_for_display)

    with pytest.raises(TimeoutError, match="stalled"):
        runner._repl_wait_selection(pty, runtime, timeout=4.0)

    assert runtime.repl_candidate_wait_count == 1


def test_repl_post_rollback_selection_preserves_stall_failure_without_restart(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        transcript = "prior terminal output"

        def terminate(self, *, force):
            calls.append(("terminate", force))

        def spawn(self, *, extra_args):
            calls.append(("spawn", extra_args))

    def wait_selection(_pty, runtime, **kwargs):
        calls.append(("wait", kwargs))
        if len([item for item in calls if item[0] == "wait"]) == 1:
            raise TimeoutError("stalled")
        runtime.repl_candidate_wait_count += 1

    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=900.0),
        repl_candidate_wait_count=1,
        checks={"REPL display candidate_selection_ready occurrence 2 observed": False},
        diagnostics={},
        watchdog={"state": "no_output"},
    )
    monkeypatch.setattr(runner, "_repl_wait_selection", wait_selection)
    monkeypatch.setattr(runner, "_repl_active_deploy_step", lambda _runtime: False)

    with pytest.raises(TimeoutError, match="stalled"):
        runner._repl_wait_selection_after_rollback(runtime, Pty())

    assert len(calls) == 1
    assert calls[0][0] == "wait"
    assert "只在杭州创建一个最小测试安全组" in calls[0][1]["clarification_answer"]
    assert runtime.diagnostics == {}
    assert runtime.checks == {"REPL display candidate_selection_ready occurrence 2 observed": False}


def test_repl_post_rollback_selection_does_not_restart_active_planning(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=900.0),
        repl_candidate_wait_count=1,
        watchdog={"state": "running"},
    )

    def wait_selection(*_args, **kwargs):
        assert set(kwargs) == {"clarification_answer"}
        raise TimeoutError("overall deadline")

    monkeypatch.setattr(runner, "_repl_wait_selection", wait_selection)
    monkeypatch.setattr(runner, "_repl_active_deploy_step", lambda _runtime: False)

    with pytest.raises(TimeoutError, match="overall deadline"):
        runner._repl_wait_selection_after_rollback(runtime, object())


def test_repl_candidate_waiting_restart_uses_durable_events_and_handoff_delay(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def terminate(self, *, force: bool = False) -> None:
            calls.append(("terminate", force))

        def spawn(self, *, extra_args: list[str]) -> None:
            calls.append(("spawn", extra_args))

        def drain_output(self) -> None:
            calls.append("drain")

    monkeypatch.setattr(runner, "_repl_wait_selection", lambda _pty, _runtime: calls.append("selection"))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._restart_repl_at_waiting(
        Pty(),
        runner.REPL_SELECTION_PATTERNS,
        argparse.Namespace(),
        "candidate selection",
    )

    assert calls == [
        "selection",
        ("terminate", True),
        ("spawn", ["--continue"]),
        "selection",
        ("sleep", 0.5),
        "drain",
    ]


def test_repl_confirmation_restart_waits_for_ready_hint_only_once(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", patterns, description, timeout))
            return patterns[0]

        def terminate(self, *, force: bool = False) -> None:
            calls.append(("terminate", force))

        def spawn(self, *, extra_args: list[str]) -> None:
            calls.append(("spawn", extra_args))

    monkeypatch.setattr(
        runner,
        "_prepare_restored_repl_confirmation",
        lambda _pty, _runtime: calls.append("prepare-confirmation"),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation_after_optional_parameter_asks",
        lambda _pty, _runtime: calls.append("wait-confirmation"),
    )
    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))

    runner._restart_repl_at_waiting(
        Pty(),
        runner.REPL_CONFIRMATION_PATTERNS,
        runtime,
        "deployment confirmation",
    )

    assert calls == [
        "wait-confirmation",
        ("terminate", True),
        ("spawn", ["--continue"]),
        "prepare-confirmation",
    ]


def test_repl_confirmation_cost_details_only_expected_for_priced_resources(runner: ModuleType) -> None:
    event = {
        "type": "user_input_required",
        "step_id": runner.NEW_STEPS[1],
        "payload": {"kind": "deployment_confirmation", "cost": {"resources": []}},
    }
    assert runner._repl_confirmation_has_cost_lines([event]) is False
    event["payload"]["cost"]["resources"] = [{"type": "VSwitch", "cost": "¥1/月"}]
    assert runner._repl_confirmation_has_cost_lines([event]) is True


def test_repl_step_started_wait_filters_by_target_step(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
                {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
                {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = argparse.Namespace(events=[])

    runner._repl_wait_step_started(
        pty,
        runtime,
        step_id=runner.NEW_STEPS[1],
        occurrence=2,
        description="resumed Step 2",
    )

    assert pty.events[0]["description"] == "resumed Step 2"
    assert pty.events[0]["step_id"] == runner.NEW_STEPS[1]
    assert pty.events[0]["occurrence"] == 2


def test_repl_running_checkpoint_uses_target_step_persisted_tool_use(runner: ModuleType, tmp_path: Path) -> None:
    pipeline_dir = tmp_path / "config" / "projects" / "project" / "session" / "pipeline"
    transcript_path = pipeline_dir / "transcripts" / "transcript_att_0002" / "session.jsonl"
    transcript_path.parent.mkdir(parents=True)
    (pipeline_dir / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "attempts": {
                    "items": {
                        "att_0001": {
                            "step_id": runner.NEW_STEPS[0],
                            "transcript_id": "transcript_att_0001",
                        },
                        "att_0002": {
                            "step_id": runner.NEW_STEPS[1],
                            "transcript_id": "transcript_att_0002",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    transcript_path.write_text(
        json.dumps(
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "write_file", "id": "call-step2"},
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def drain_output(self) -> None:
            return None

    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = Pty()

    runner._wait_repl_transcript_tool_use(
        pty,
        runtime,
        step_id=runner.NEW_STEPS[1],
        tool_names={"write_file"},
        description="Step 2 template checkpoint",
    )

    assert pty.events[0]["type"] == "transcript-tool-use"
    assert pty.events[0]["step_id"] == runner.NEW_STEPS[1]
    assert pty.events[0]["tool_name"] == "write_file"
    assert pty.events[0]["tool_use_id"] == "call-step2"
    assert pty.events[0]["path"] == str(transcript_path)


@pytest.mark.parametrize("allow_free_text", [True, False])
def test_repl_running_step2_answers_native_question_before_template_fault(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_free_text: bool,
) -> None:
    directory = tmp_path / "projects/project/session/pipeline"
    transcript = directory / "transcripts/step2/session.jsonl"
    transcript.parent.mkdir(parents=True)
    meta = directory / "meta.yaml"
    metadata = {"current_step": runner.NEW_STEPS[1], "attempts": {"items": {
        "attempt": {"step_id": runner.NEW_STEPS[1], "transcript_id": "step2"},
    }}, "execution": {"pending_input_kind": "ask_user_question", "pending_ask_user_question_input": {
        "toolUseId": "parameter-question", "question": "Choose a valid test subnet?",
        "allowFreeText": allow_free_text, "options": [{"id": "subnet", "label": "Test subnet"}],
    }}}
    meta.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=0.1), checks={}, diagnostics={})
    calls = []
    pty = SimpleNamespace(events=[], drain_output=lambda: None, transcript="")

    def answer(_runtime, payload):
        assert payload["tool_use_id"] == "parameter-question"
        calls.append("question driver")
        return "10.250.1.0/24" if allow_free_text else "subnet"

    def submit(_pty, _runtime, text, pending, *, label):
        assert text == ("10.250.1.0/24" if allow_free_text else "1")
        assert pending[0]["payload"]["tool_use_id"] == "parameter-question"
        calls.append("answer acknowledged")
        metadata["execution"] = {"pending_input_kind": None}
        meta.write_text(yaml.safe_dump(metadata), encoding="utf-8")
        transcript.write_text(json.dumps({"message": {"content": [{
            "type": "tool_use", "name": "write_file", "id": "actual-template-call",
        }]}}) + "\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_answer_runtime_question", answer)
    monkeypatch.setattr(runner, "_repl_wait_ask", lambda *_, **__: calls.append("input ready"))
    monkeypatch.setattr(runner, "_repl_submit_question_answer", submit)
    runner._wait_repl_transcript_tool_use(
        pty, runtime, step_id=runner.NEW_STEPS[1], tool_names={"write_file"},
        description="original template checkpoint",
    )

    assert calls == ["question driver", "input ready", "answer acknowledged"]
    assert pty.events[-1]["type"] == "transcript-tool-use"
    assert pty.events[-1]["tool_use_id"] == "actual-template-call"
    assert runtime.diagnostics["repl_tool_checkpoint_parameter_asks"] == 1


def test_repl_running_checkpoint_question_does_not_extend_original_deadline(runner, tmp_path, monkeypatch):
    clock = [0.0]
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1.0), checks={}, diagnostics={})
    pty = SimpleNamespace(events=[], drain_output=lambda: None, transcript="")
    pending = ({"payload": {"tool_use_id": "question", "allow_free_text": True}}, tmp_path / "meta.yaml")
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner, "_pending_repl_parameter_question", lambda *_, **__: pending)
    monkeypatch.setattr(runner, "_answer_runtime_question", lambda *_: "test answer")
    monkeypatch.setattr(runner, "_repl_wait_ask", lambda *_, **__: None)
    monkeypatch.setattr(runner, "_repl_submit_question_answer", lambda *_, **__: clock.__setitem__(0, 2.0))

    with pytest.raises(TimeoutError, match="original template checkpoint"):
        runner._wait_repl_transcript_tool_use(
            pty, runtime, step_id=runner.NEW_STEPS[1], tool_names={"write_file"},
            description="original template checkpoint",
        )
    assert not pty.events


def test_repl_running_checkpoint_question_budget_does_not_replace_tool_proof(runner, tmp_path, monkeypatch):
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1.0), checks={}, diagnostics={})
    pty = SimpleNamespace(events=[], drain_output=lambda: None, transcript="")
    monkeypatch.setattr(runner, "_pending_repl_parameter_question", lambda _runtime, answered, **__: (
        {"payload": {"tool_use_id": f"question-{len(answered)}", "allow_free_text": True}},
        tmp_path / "meta.yaml",
    ))
    monkeypatch.setattr(runner, "_answer_runtime_question", lambda *_: "test answer")
    monkeypatch.setattr(runner, "_repl_wait_ask", lambda *_, **__: None)
    monkeypatch.setattr(runner, "_repl_submit_question_answer", lambda *_, **__: None)

    with pytest.raises(RuntimeError, match="question budget exhausted"):
        runner._wait_repl_transcript_tool_use(
            pty, runtime, step_id=runner.NEW_STEPS[1], tool_names={"write_file"},
            description="original template checkpoint",
        )
    assert runtime.diagnostics["repl_tool_checkpoint_parameter_asks"] == 8
    assert not pty.events


def test_repl_running_step2_checkpoint_rejects_already_reached_confirmation(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        json.dumps(
            {
                "type": "user_input_required",
                "step_id": runner.NEW_STEPS[1],
                "payload": {"kind": "deployment_confirmation"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = argparse.Namespace(drain_output=lambda: None, events=[])

    with pytest.raises(RuntimeError, match="deployment confirmation"):
        runner._wait_repl_transcript_tool_use(
            pty,
            runtime,
            step_id=runner.NEW_STEPS[1],
            tool_names={"write_file"},
            description="Step 2 template checkpoint",
        )


def test_repl_running_step3_answers_native_question_before_original_deployment_fault(runner, tmp_path, monkeypatch):
    """Run 77637291 had a real Step 2 ask, rather than a deployment confirmation."""
    directory = tmp_path / "projects/project/session/pipeline"
    directory.mkdir(parents=True)
    display = directory / "display.jsonl"
    events = [{"type": "candidate_selection_submitted", "step_id": runner.NEW_STEPS[0]},
              {"type": "step_started", "step_id": runner.NEW_STEPS[1]}]
    display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    meta = directory / "meta.yaml"
    meta.write_text(yaml.safe_dump({"current_step": runner.NEW_STEPS[1], "execution": {
        "pending_input_kind": "ask_user_question", "pending_ask_user_question_input": {
            "toolUseId": "actual-question", "question": "Confirm the known test CIDR?", "options": [],
        },
    }}), encoding="utf-8")
    calls = []

    class Pty:
        def __init__(self, **_kwargs):
            self.events = []
            self.transcript = ""

        def spawn(self, *, extra_args=None):
            calls.append(("spawn", extra_args))

        def terminate(self, *, force=False):
            calls.append(("terminate", force))

        def drain_output(self):
            pass

        def send(self, text, *, label):
            calls.append(("send", text, label))

    runtime = SimpleNamespace(spec=SimpleNamespace(profile="running_step3", cloud_write=True),
        args=SimpleNamespace(stream_timeout=0.05), env={},
        paths=SimpleNamespace(config_dir=tmp_path, run_dir=tmp_path, workspace_dir=tmp_path),
        checks={}, diagnostics={}, repl_confirmation_wait_count=0, repl_candidate_wait_count=1,
        repl_confirmation_action_count=0, watchdog=None, event=lambda *_, **__: None)
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: SimpleNamespace(
        ReplPty=Pty, _expect_initial_prompt=lambda *_: None))
    monkeypatch.setattr(runner, "_python_namespace", lambda _: SimpleNamespace())
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_: None)
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_: None)
    monkeypatch.setattr(runner, "_repl_select_current", lambda *_: None)
    monkeypatch.setattr(runner, "_repl_wait_ask", lambda *_, **__: None)
    monkeypatch.setattr(runner, "_answer_runtime_question", lambda _, payload: "10.250.1.0/24")
    def answer(_pty, _runtime, text, pending, *, label):
        assert pending[0]["payload"]["tool_use_id"] == "actual-question"
        assert text == "10.250.1.0/24"
        calls.append("answer")
        meta.write_text(yaml.safe_dump({"current_step": runner.NEW_STEPS[1], "execution": {
            "pending_input_kind": "deployment_confirmation",
        }}), encoding="utf-8")
        events.append({"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
            "kind": "deployment_confirmation", "options": [{"action": "confirm"}, {"action": "cancel"}],
        }})
        display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    monkeypatch.setattr(runner, "_repl_submit_question_answer", answer)
    monkeypatch.setattr(runner, "_repl_wait_step_started", lambda *_, **__: None)
    def checkpoint(_pty, _runtime, **kwargs):
        assert kwargs["step_id"] == runner.NEW_STEPS[2] and kwargs["tool_names"] == {"ros_deploy"}
        assert "answer" in calls and ("send", "\r", "confirmation-confirm") in calls
        calls.append("original deployment checkpoint")
    monkeypatch.setattr(runner, "_wait_repl_transcript_tool_use", checkpoint)
    monkeypatch.setattr(runner, "_repl_wait_pipeline_completed", lambda *_: calls.append("completed"))
    monkeypatch.setattr(runner, "_write_repl_artifacts", lambda *_: None)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    runner._run_repl(runtime)
    assert calls.index("original deployment checkpoint") < calls.index(("terminate", True))
    assert calls.index(("terminate", True)) < calls.index(("spawn", ["--continue"])) < calls.index("completed")
    assert runtime.checks[f"{runner.NEW_STEPS[2]} auto-continued after --continue"] is True
    assert runtime.diagnostics["repl_native_parameter_asks"] == 1


def test_repl_running_checkpoint_rejects_already_terminal_pipeline(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(json.dumps({"type": "pipeline_completed"}) + "\n", encoding="utf-8")
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=1.0),
    )
    pty = argparse.Namespace(drain_output=lambda: None, events=[])

    with pytest.raises(RuntimeError, match="pipeline_completed"):
        runner._wait_repl_transcript_tool_use(
            pty,
            runtime,
            step_id=runner.NEW_STEPS[2],
            tool_names={"ros_deploy"},
            description="Step 3 deployment checkpoint",
        )


def test_repl_running_step1_resume_waits_on_candidate_boundary_without_second_step_start(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    class Pty:
        def __init__(self, **_kwargs: object) -> None:
            self.events: list[dict[str, object]] = []
            self.transcript = ""

        def spawn(self, *, extra_args: list[str] | None = None) -> None:
            calls.append(("spawn", extra_args))

        def terminate(self, *, force: bool = False) -> None:
            calls.append(("terminate", force))

        def drain_output(self) -> None:
            calls.append("drain")

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    fake_repl = argparse.Namespace(ReplPty=Pty, _expect_initial_prompt=lambda *_args: calls.append("ready"))
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=1.0),
        env={},
        paths=argparse.Namespace(run_dir=tmp_path, workspace_dir=tmp_path, config_dir=tmp_path),
        spec=argparse.Namespace(profile="running_step1", cloud_write=False),
        checks={},
        repl_candidate_wait_count=0,
        event=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: calls.append("initial"))
    monkeypatch.setattr(
        runner,
        "_repl_wait_step_started",
        lambda *_args, **kwargs: calls.append(("step-started", kwargs["occurrence"])),
    )
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args, **_kwargs: calls.append("selection"))
    monkeypatch.setattr(runner, "_repl_select_current", lambda *_args: calls.append("select"))
    monkeypatch.setattr(runner, "_repl_wait_confirmation_after_optional_parameter_asks",
                        lambda *_args: calls.append("confirmation"))
    monkeypatch.setattr(runner, "_repl_choose_direct_input", lambda *_args: calls.append("cancel"))
    monkeypatch.setattr(runner, "_repl_wait_pipeline_completed", lambda *_args: calls.append("completed"))
    monkeypatch.setattr(runner, "_write_repl_artifacts", lambda *_args: calls.append("artifacts"))
    monkeypatch.setattr(runner.time, "sleep", lambda *_args: None)

    runner._run_repl(runtime)

    assert [item for item in calls if isinstance(item, tuple) and item[0] == "step-started"] == [("step-started", 1)]
    assert ("spawn", ["--continue"]) in calls
    assert calls.index("selection") > calls.index(("spawn", ["--continue"]))
    assert runtime.checks[f"{runner.NEW_STEPS[0]} auto-continued after --continue"] is True


def test_repl_display_wait_fails_fast_on_terminal_pipeline_event(runner: ModuleType, tmp_path: Path) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(json.dumps({"type": "pipeline_user_aborted"}) + "\n", encoding="utf-8")
    runtime = argparse.Namespace(paths=argparse.Namespace(config_dir=tmp_path / "config"))

    with pytest.raises(RuntimeError, match="pipeline_user_aborted.*candidate_selection_ready"):
        runner._wait_repl_display_event(
            runtime,
            event_type="candidate_selection_ready",
            occurrence=1,
            timeout=1.0,
        )


@pytest.mark.parametrize(
    ("transcript", "elapsed", "aborts"),
    [("", 601.0, True), ("CreateStack", 601.0, False), ("CreateStack", 1501.0, True)],
)
def test_repl_file_wait_uses_output_idle_guard(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    transcript: str,
    elapsed: float,
    aborts: bool,
) -> None:
    pty = argparse.Namespace(_last_output_at=0.0, transcript=transcript, events=[], _wait_diagnoses=[])
    runtime = argparse.Namespace(watchdog=None)
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(WAIT_IDLE_SECONDS=600.0, WAIT_CLOUD_IDLE_SECONDS=1500.0),
    )
    monkeypatch.setattr(runner.time, "monotonic", lambda: elapsed)

    if aborts:
        with pytest.raises(TimeoutError, match="no terminal output"):
            runner._observe_repl_wait(
                pty, runtime, description="REPL display pipeline_completed occurrence 1",
                started=0.0, transcript_offset=0, diagnosis_attempted=False,
            )
        assert runtime.watchdog["action"] == "early_abort"
        assert pty._wait_diagnoses[-1] == runtime.watchdog
    else:
        assert runner._observe_repl_wait(
            pty, runtime, description="REPL display pipeline_completed occurrence 1",
            started=0.0, transcript_offset=0, diagnosis_attempted=False,
        ) is True
        assert runtime.watchdog is None


def test_repl_file_wait_ignores_old_cloud_text_but_keeps_active_deploy(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pty = argparse.Namespace(_last_output_at=0.0, transcript="CreateStack", events=[], _wait_diagnoses=[])
    runtime = argparse.Namespace(watchdog=None)
    monkeypatch.setattr(
        runner, "_legacy_repl_module",
        lambda: argparse.Namespace(WAIT_IDLE_SECONDS=600.0, WAIT_CLOUD_IDLE_SECONDS=1500.0),
    )
    monkeypatch.setattr(runner.time, "monotonic", lambda: 601.0)
    with pytest.raises(TimeoutError, match="no terminal output"):
        runner._observe_repl_wait(
            pty, runtime, description="confirmation", started=0.0,
            transcript_offset=len(pty.transcript), diagnosis_attempted=False,
        )

    display = tmp_path / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    display.write_text('{"type":"step_started","step_id":"deploying"}\n', encoding="utf-8")
    runtime.paths = argparse.Namespace(config_dir=tmp_path)
    assert runner._observe_repl_wait(
        pty, runtime, description="pipeline completed", started=0.0,
        transcript_offset=len(pty.transcript), diagnosis_attempted=True,
    ) is True


def test_repl_file_wait_counts_persisted_step_progress_as_activity(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transcript = (
        tmp_path / "projects" / "project" / "session" / "pipeline" / "transcripts" / "attempt" / "session.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"tool_use"}\n', encoding="utf-8")
    pty = argparse.Namespace(_last_output_at=0.0, transcript="", events=[], _wait_diagnoses=[])
    runtime = argparse.Namespace(paths=argparse.Namespace(config_dir=tmp_path), watchdog=None)
    clock = [0.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        runner, "_legacy_repl_module",
        lambda: argparse.Namespace(WAIT_IDLE_SECONDS=600.0, WAIT_CLOUD_IDLE_SECONDS=1500.0),
    )

    runner._observe_repl_wait(
        pty, runtime, description="Step 2 confirmation", started=0.0,
        transcript_offset=0, diagnosis_attempted=True,
    )
    transcript.write_text('{"type":"tool_use"}\n{"type":"tool_result"}\n', encoding="utf-8")
    clock[0] = 601.0

    assert runner._observe_repl_wait(
        pty, runtime, description="Step 2 confirmation", started=0.0,
        transcript_offset=0, diagnosis_attempted=True,
    ) is True
    assert runtime.watchdog is None


def test_repl_file_wait_records_advisory_diagnosis(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {
        "state": "normal_operation", "action": "observe", "waitingFor": "REPL display pipeline_completed occurrence 1",
        "elapsedSeconds": 121.0, "cue": "none",
    }
    pty = argparse.Namespace(_last_output_at=120.0, transcript="working", events=[], _wait_diagnoses=[])
    calls: list[tuple[str, int, float]] = []

    def diagnose(description: str, offset: int, elapsed: float) -> bool:
        calls.append((description, offset, elapsed))
        pty._wait_diagnoses.append(record)
        return True

    pty._diagnose_wait = diagnose
    runtime = argparse.Namespace(watchdog=None)
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(WAIT_IDLE_SECONDS=600.0, WAIT_CLOUD_IDLE_SECONDS=1500.0),
    )
    monkeypatch.setattr(runner.time, "monotonic", lambda: 121.0)

    assert runner._observe_repl_wait(
        pty, runtime, description=record["waitingFor"], started=0.0,
        transcript_offset=3, diagnosis_attempted=False,
    ) is True
    assert calls == [(record["waitingFor"], 3, 121.0)]
    assert runtime.watchdog == record


def test_repl_candidate_switch_waits_for_arrow_before_enter(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str] | str] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            sent.append((text, label))

        def drain_output(self) -> None:
            sent.append("drain")

    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: sent.append("settle"))
    runner._repl_select_current(Pty(), next_candidate=True)

    assert sent == [("\x1b[C", "candidate-right"), "settle", "drain", ("\r", "candidate-enter")]


def test_repl_candidate_enter_retries_until_submission_is_recorded(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    ticks = iter(range(100))

    class Pty:
        def send(self, _text: str, *, label: str) -> None:
            sent.append(label)

        def drain_output(self) -> None:
            pass

    monkeypatch.setattr(runner, "_repl_selection_submission_count", lambda _pty: int(len(sent) >= 2))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    runner._repl_select_current(Pty())

    assert sent == ["candidate-enter", "candidate-enter-retry-2"]


def test_repl_restored_line_input_uses_paste_then_separate_enter(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_line_input(Pty(), "杭州 VSwitch", label="answer")

    assert calls == [
        ("send", "\x1b[200~杭州 VSwitch\x1b[201~", "answer-paste"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "answer-enter"),
    ]


def test_repl_pipeline_interrupt_waits_for_editor_before_submitting(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    fake_repl = argparse.Namespace(_expect_interrupt_input_ready=lambda *_args, **_kwargs: calls.append("ready"))
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_pipeline_interrupt(Pty(), argparse.Namespace(), "改为只创建空 VPC")

    assert calls == [
        ("send", "\x1b", "pipeline-stream-interrupt"),
        "ready",
        ("sleep", 0.25),
        "drain",
        ("send", "\x1b[200~改为只创建空 VPC\x1b[201~", "pipeline-stream-interrupt-input-paste"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "pipeline-stream-interrupt-input-enter"),
    ]


def test_repl_direct_input_focuses_editable_row_before_typing(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            events.append((text, label))

        def drain_output(self) -> None:
            events.append("drain")

    runtime = argparse.Namespace(repl_confirmation_action_count=3)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    runner._repl_choose_direct_input(runtime, Pty(), "调整参数")

    assert events == [
        ("\x1b[B", "confirmation-input-down-1"),
        ("\x1b[B", "confirmation-input-down-2"),
        ("\x1b[B", "confirmation-input-down-3"),
        ("\x1b[200~调整参数\x1b[201~", "confirmation-direct-input-paste"),
        ("sleep", 0.1),
        "drain",
        ("\r", "confirmation-direct-input-enter"),
    ]


def test_repl_direct_image_focuses_editable_row_and_submits_after_paste(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(repl_confirmation_action_count=2)
    monkeypatch.setattr(
        runner,
        "_repl_paste_generated_image",
        lambda _runtime, _pty, key, text: calls.append(("image", key, text)),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_choose_direct_image(runtime, Pty(), "adjustment", "调整 VSwitch 网段")

    assert calls == [
        ("send", "\x1b[B", "confirmation-input-down-1"),
        ("send", "\x1b[B", "confirmation-input-down-2"),
        ("image", "adjustment", "调整 VSwitch 网段"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "confirmation-direct-image-enter"),
    ]


def test_repl_image_fixture_uses_separate_enter_after_refresh(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def paste_image_fixture(self, key: str) -> None:
            calls.append(("fixture", key))

        def drain_output(self) -> None:
            calls.append("drain")

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_image_fixture(Pty(), "normal-followup", label="normal-image-enter")

    assert calls == [
        ("fixture", "normal-followup"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "normal-image-enter"),
    ]


def test_repl_generated_image_uses_separate_enter_after_refresh(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def drain_output(self) -> None:
            calls.append("drain")

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    monkeypatch.setattr(
        runner,
        "_repl_paste_generated_image",
        lambda _runtime, _pty, key, text: calls.append(("image", key, text)),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_generated_image(
        argparse.Namespace(),
        Pty(),
        "initial",
        "方案选定后必须由我选择 VPC",
        label="initial-image-enter",
    )

    assert calls == [
        ("image", "initial", "方案选定后必须由我选择 VPC"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "initial-image-enter"),
    ]


def test_repl_multimodal_selection_retries_until_durable_submission(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels: list[str] = []
    counts = iter((1, 2))
    ticks = iter((0.0, 6.0, 10.0, 11.0))
    runtime = argparse.Namespace(diagnostics={})
    pty = argparse.Namespace(drain_output=lambda: None)
    monkeypatch.setattr(
        runner, "_legacy_repl_module",
        lambda: argparse.Namespace(_repl_selection_submission_count=lambda _pty: next(counts)),
    )
    monkeypatch.setattr(runner, "_repl_submit_image_fixture", lambda _pty, _key, *, label: labels.append(label))
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))

    runner._repl_submit_multimodal_selection(runtime, pty, label="rollback-selection-image-enter")

    assert labels == ["rollback-selection-image-enter", "rollback-selection-image-enter-retry-2"]
    assert runtime.diagnostics["repl_selection_image_retries"] == 1


def test_repl_confirmation_records_action_count_from_display(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    display_path = tmp_path / "config" / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display_path.parent.mkdir(parents=True)
    display_path.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "type": "user_input_required",
                    "step_id": runner.NEW_STEPS[1],
                    "payload": {
                        "kind": "deployment_confirmation",
                        "options": [{"action": "confirm"}, {"action": "cancel"}],
                    },
                },
                {
                    "type": "user_input_required",
                    "step_id": runner.NEW_STEPS[1],
                    "payload": {
                        "kind": "deployment_confirmation",
                        "options": [
                            {"action": "confirm"},
                            {"action": "reselect"},
                            {"action": "cancel"},
                        ],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def expect_any(self, patterns, *, description, timeout):
            assert patterns == runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS
            assert description == "deployment confirmation selector ready #2"
            assert timeout == 9.0
            calls.append("expect")
            return patterns[0]

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path / "config"),
        args=argparse.Namespace(stream_timeout=9.0),
        repl_confirmation_wait_count=1,
        repl_confirmation_action_count=0,
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    pty = Pty()

    runner._repl_wait_confirmation(pty, runtime)

    assert calls == ["expect", "drain"]
    assert runtime.repl_confirmation_wait_count == 2
    assert runtime.repl_confirmation_action_count == 3
    assert pty.events[0]["occurrence"] == 2


@pytest.mark.parametrize('free_text', [True, False])
def test_repl_completion_answers_real_pending_question_without_changing_milestone(
    runner, monkeypatch, tmp_path, free_text
):
    meta = tmp_path / 'projects/p/s/pipeline/meta.yaml'
    meta.parent.mkdir(parents=True)
    meta.write_text(yaml.safe_dump({'current_step': runner.NEW_STEPS[1], 'execution': {
        'pending_input_kind': 'ask_user_question', 'pending_ask_user_question_input': {
            'toolUseId': 'question-1', 'question': 'Keep the adjusted subnet?',
            'allowFreeText': free_text, 'options': [{'id': 'keep', 'label': 'Keep the requested subnet'}],
        }}}), encoding='utf-8')
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=30), checks={'original acceptance': True})
    pty = SimpleNamespace(events=[], drain_output=lambda: None)
    answers, timeouts = [], []
    def wait(_runtime, **kwargs):
        assert kwargs['event_type'] == 'pipeline_completed' and kwargs['occurrence'] == 1
        timeouts.append(kwargs['timeout'])
        pending = kwargs.get('alternate_input', lambda: None)()
        if pending:
            return pending
        if not answers:
            raise RuntimeError('unhandled pending question while waiting for completion')
        return {'type': 'pipeline_completed'}, meta.with_name('display.jsonl')
    def submit(_pty, _runtime, text, pending, *, label):
        assert pending[0]['payload']['tool_use_id'] == 'question-1'
        answers.append(text)
        meta.write_text(yaml.safe_dump({'current_step': runner.NEW_STEPS[2], 'execution': {}}), encoding='utf-8')
    monkeypatch.setattr(runner, '_wait_repl_display_event', wait)
    monkeypatch.setattr(
        runner, '_answer_runtime_question', lambda *_: 'keep' if not free_text else 'keep requested CIDR'
    )
    monkeypatch.setattr(runner, '_repl_wait_ask', lambda *_, **__: None)
    monkeypatch.setattr(runner, '_repl_submit_question_answer', submit)
    runner._repl_wait_pipeline_completed(pty, runtime)
    assert answers == ['keep requested CIDR' if free_text else '1']
    assert 0 < timeouts[1] <= timeouts[0] <= 30
    assert runtime.checks == {'original acceptance': True}
    assert pty.events[-1]['event_type'] == 'pipeline_completed'


def test_repl_completion_fails_at_native_replanning_without_reselecting(runner, monkeypatch, tmp_path):
    display = tmp_path / 'projects/p/s/pipeline/display.jsonl'
    display.parent.mkdir(parents=True)
    display.write_text('\n'.join(json.dumps(row) for row in [
        {'type': 'step_started', 'step_id': runner.NEW_STEPS[0]},
        {'type': 'step_started', 'step_id': runner.NEW_STEPS[1]},
        {'type': 'step_started', 'step_id': runner.NEW_STEPS[2]},
        {'type': 'step_completed', 'step_id': runner.NEW_STEPS[2]},
        {'type': 'step_started', 'step_id': runner.NEW_STEPS[0]},
        {'type': 'candidate_selection_ready', 'step_id': runner.NEW_STEPS[0]},
    ]), encoding='utf-8')
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=30), checks={})
    pty = SimpleNamespace(events=[], drain_output=lambda: None)
    def wait(_runtime, **kwargs):
        kwargs['alternate_input']()
        raise TimeoutError('would wait entire deadline')
    monkeypatch.setattr(runner, '_wait_repl_display_event', wait)
    with pytest.raises(RuntimeError, match='returned to planning after deployment'):
        runner._repl_wait_pipeline_completed(pty, runtime)
    assert runtime.checks['REPL display pipeline_completed occurrence 1 observed'] is False
    assert pty.events == []


def test_repl_completion_repeated_questions_exhaust_budget_without_fake_completion(runner, monkeypatch):
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=30), checks={})
    pty = SimpleNamespace(events=[], drain_output=lambda: None)
    answers = []
    def wait(_runtime, **kwargs):
        return {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1], 'payload': {
            'kind': 'ask_user_question', 'allow_free_text': True, 'tool_use_id': str(len(answers))}}, Path('meta')
    monkeypatch.setattr(runner, '_wait_repl_display_event', wait)
    monkeypatch.setattr(runner, '_answer_runtime_question', lambda *_: 'unchanged user intent')
    monkeypatch.setattr(runner, '_repl_wait_ask', lambda *_, **__: None)
    monkeypatch.setattr(runner, '_repl_submit_question_answer', lambda *args, **_: answers.append(args[2]))
    with pytest.raises(RuntimeError, match='question budget exhausted'):
        runner._repl_wait_pipeline_completed(pty, runtime)
    assert len(answers) == 8 and pty.events == []


def test_repl_completion_keeps_waiting_for_current_deployment_after_old_rollback(runner, monkeypatch, tmp_path):
    display = tmp_path / 'projects/p/s/pipeline/display.jsonl'
    display.parent.mkdir(parents=True)
    display.write_text('\n'.join(json.dumps({'type': 'step_started', 'step_id': step})
                                 for step in [*runner.NEW_STEPS, *runner.NEW_STEPS]), encoding='utf-8')
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=30), checks={})
    pty = SimpleNamespace(events=[], drain_output=lambda: None)
    def wait(_runtime, **kwargs):
        assert kwargs['alternate_input']() is None
        return {'type': 'pipeline_completed'}, display
    monkeypatch.setattr(runner, '_wait_repl_display_event', wait)
    runner._repl_wait_pipeline_completed(pty, runtime)
    assert runtime.checks == {}
    assert pty.events[-1]['event_type'] == 'pipeline_completed'


def test_repl_recovery_confirmation_uses_durable_event_without_rematching_drained_hint(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    event = {
        "type": "user_input_required",
        "step_id": runner.NEW_STEPS[1],
        "payload": {
            "kind": "deployment_confirmation",
            "options": [{"action": "confirm"}, {"action": "cancel"}],
        },
    }

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def expect_any(self, *_args, **_kwargs):
            raise AssertionError("recovery must not rematch an already-drained Live hint")

        def drain_output(self) -> None:
            calls.append("drain")

    monkeypatch.setattr(runner, "_wait_repl_display_event", lambda *_args, **_kwargs: (event, Path("display")))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=9.0),
        repl_confirmation_wait_count=0,
        repl_confirmation_action_count=0,
    )
    pty = Pty()

    runner._repl_wait_confirmation(pty, runtime, require_input_ready=False)

    assert calls == [("sleep", 0.5), "drain"]
    assert runtime.repl_confirmation_action_count == 2
    assert pty.events[0]["event_type"] == "user_input_required"


def test_repl_post_rollback_confirmation_does_not_count_received_answers(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    display = tmp_path / "projects" / "project" / "session" / "pipeline" / "display.jsonl"
    display.parent.mkdir(parents=True)
    events = [
        {"type": "candidate_selection_submitted"},
        {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
            "kind": "deployment_confirmation", "options": [{"action": "confirm"}, {"action": "cancel"}],
        }},
        {"type": "user_input_received", "step_id": runner.NEW_STEPS[1], "payload": {
            "kind": "deployment_confirmation", "selected_value": "change architecture",
        }},
        {"type": "candidate_selection_submitted"},
        {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
            "kind": "deployment_confirmation",
            "options": [{"action": "confirm"}, {"action": "reselect"}, {"action": "cancel"}],
        }},
    ]
    runtime = argparse.Namespace(spec=argparse.Namespace(profile="rollback"),
        paths=argparse.Namespace(config_dir=tmp_path),
        args=argparse.Namespace(stream_timeout=0.01),
        checks={}, repl_confirmation_wait_count=1, repl_confirmation_action_count=0,
    )
    pty = argparse.Namespace(events=[], drain_output=lambda: None)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    for occurrence in (2, 3):
        display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        runner._repl_wait_confirmation_after_optional_parameter_asks(pty, runtime)
        assert runtime.repl_confirmation_wait_count == occurrence
        assert runtime.repl_confirmation_action_count == 3
        assert pty.events[-1]["occurrence"] == occurrence
        events.extend([
            {"type": "user_input_received", "step_id": runner.NEW_STEPS[1], "payload": {
                "kind": "deployment_confirmation", "action": "confirm",
            }},
            {"type": "candidate_selection_submitted"},
            {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
                "kind": "deployment_confirmation",
                "options": [{"action": "confirm"}, {"action": "reselect"}, {"action": "cancel"}],
            }},
        ])
    assert runtime.checks == {}


def test_repl_post_rollback_confirmation_answers_parameter_ask_first(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    inputs = iter(
        [
            {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {"kind": "ask_user_question"}},
            {
                "type": "user_input_required",
                "step_id": runner.NEW_STEPS[1],
                "payload": {"kind": "deployment_confirmation"},
            },
        ]
    )

    class Pty:
        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=9.0, cleanup_vpc_id="vpc-test"),
        spec=argparse.Namespace(profile="rollback"),
    )
    monkeypatch.setattr(
        runner, "_read_repl_display_events", lambda _runtime: [
            {"type": "user_input_required", "step_id": runner.NEW_STEPS[1],
             "payload": {"kind": "deployment_confirmation"}},
            {"type": "candidate_selection_submitted"},
        ],
    )
    def wait_input(_runtime, **kwargs):
        calls.append(("durable", kwargs["occurrence"]))
        return next(inputs), Path("display")

    monkeypatch.setattr(runner, "_wait_repl_display_event", wait_input)
    monkeypatch.setattr(
        runner, "_repl_wait_ask",
        lambda _pty, _runtime, *, description, allow_captured_prompt: calls.append(("ask", description)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_submit_line_input",
        lambda _pty, text, *, label: calls.append(("answer", text, label)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    monkeypatch.setattr(runner, "_answer_runtime_question", lambda *_args, **_kw: "vpc-test")
    monkeypatch.setattr(
        runner, "_repl_submit_question_answer",
        lambda _pty, _runtime, text, _pending, *, label: calls.append(("answer", text, label)),
    )
    runner._repl_wait_confirmation_after_optional_parameter_asks(Pty(), runtime)

    assert calls == [
        ("durable", 2),
        ("ask", "Step 2 parameter ask #1"),
        ("answer", "vpc-test", "step2-parameter-answer-1"),
        ("durable", 2),
        ("confirmation", False),
    ]


def test_repl_step2_wait_observes_native_question_outside_display_journal(
    runner: ModuleType, tmp_path: Path
) -> None:
    meta = tmp_path / "projects" / "project" / "session" / "pipeline" / "meta.yaml"
    meta.parent.mkdir(parents=True)
    state = {
        "current_step": runner.NEW_STEPS[1],
        "execution": {
            "pending_input_kind": "ask_user_question",
            "pending_ask_user_question_input": {
                "toolUseId": "parameter-call", "question": "Which VPC?", "options": [],
                "allowFreeText": True,
            },
        },
    }
    meta.write_text(yaml.safe_dump(state), encoding="utf-8")
    runtime = argparse.Namespace(paths=argparse.Namespace(config_dir=tmp_path), checks={})
    answered: set[str] = set()

    event, path = runner._wait_repl_display_event(
        runtime, event_type="user_input_required", occurrence=2, timeout=1,
        predicate=runner._is_repl_deployment_confirmation,
        alternate_input=lambda: runner._pending_repl_parameter_question(runtime, answered),
    )

    assert path == meta
    assert event["payload"] == {
        "kind": "ask_user_question", "tool_use_id": "parameter-call", "allow_free_text": True,
        "_step_id": runner.NEW_STEPS[1],
        "question": "Which VPC?", "options": [],
    }
    answered.add("parameter-call")
    assert runner._pending_repl_parameter_question(runtime, answered) is None
    answered.clear()
    state["execution"]["pending_ask_user_question_input"]["answer"] = {"free_text": "vpc-test"}
    meta.write_text(yaml.safe_dump(state), encoding="utf-8")
    assert runner._pending_repl_parameter_question(runtime, answered) is None
    state["execution"]["pending_ask_user_question_input"].pop("answer")
    state["current_step"] = runner.NEW_STEPS[0]
    meta.write_text(yaml.safe_dump(state), encoding="utf-8")
    assert runner._pending_repl_parameter_question(runtime, answered) is None


@pytest.mark.parametrize("acknowledged", [True, False])
def test_restored_question_answer_waits_for_checkpoint_ack_before_next_question(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acknowledged: bool,
) -> None:
    meta = tmp_path / "projects/p/s/pipeline/meta.yaml"
    meta.parent.mkdir(parents=True)
    state = {"current_step": runner.NEW_STEPS[1], "execution": {
        "pending_input_kind": "ask_user_question", "pending_ask_user_question_input": {
            "toolUseId": "restored-parameter-call", "allowFreeText": True,
        },
    }}
    meta.write_text(yaml.safe_dump(state), encoding="utf-8")
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=0.05), checks={})
    drains: list[int] = []
    sent: list[str] = []

    class Pty:
        events = []
        def send(self, text, *, label):
            sent.append(label)

        def drain_output(self):
            drains.append(1)
            if acknowledged and len(drains) == 3:
                state["execution"]["pending_input_kind"] = None
                state["execution"]["pending_ask_user_question_input"] = None
                meta.write_text(yaml.safe_dump(state), encoding="utf-8")

    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "_answer_runtime_question", lambda *_: "vpc-test")
    if acknowledged:
        runner._repl_submit_restored_parameter_answer(Pty(), runtime)
        assert len(drains) == 3
        assert runner._pending_repl_parameter_question(runtime, set()) is None
    else:
        with pytest.raises(TimeoutError, match="answer acknowledgement"):
            runner._repl_submit_restored_parameter_answer(Pty(), runtime)
    assert runtime.checks["restored Step 2 answer acknowledged"] is acknowledged
    assert sent == ["restored Step 2 answer-paste", "restored Step 2 answer-enter"]


def test_repl_parameter_question_accepts_prompt_already_drained(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    pty = argparse.Namespace(events=[], transcript="● Ask user question: Which VPC?\n  > ",
                             drain_output=lambda: calls.append("drain"))
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=1))

    runner._repl_wait_ask(pty, runtime, description="parameter question", allow_captured_prompt=True)

    assert calls == ["drain", "drain"]
    assert pty.events[0]["description"] == "parameter question input ready"


def test_repl_rollback_selection_answers_durable_native_question_before_selection(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = tmp_path / "projects" / "project" / "session" / "pipeline" / "meta.yaml"
    meta.parent.mkdir(parents=True)
    state = {"current_step": runner.NEW_STEPS[0], "execution": {
        "pending_input_kind": "ask_user_question", "pending_ask_user_question_input": {
            "toolUseId": "planning-call", "question": "Which existing VPC?", "allowFreeText": True,
        },
    }}
    meta.write_text(yaml.safe_dump(state), encoding="utf-8")
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=tmp_path),
        args=argparse.Namespace(stream_timeout=1), repl_candidate_wait_count=1, diagnostics={},
    )
    calls: list[str] = []
    pty = argparse.Namespace(events=[], transcript="", drain_output=lambda: None)

    def wait_display(_runtime, **kwargs):
        assert kwargs["occurrence"] == 2
        assert runtime.repl_candidate_wait_count == 1
        pending = kwargs["alternate_input"]()
        if pending is not None:
            calls.append("pending question")
            return pending
        calls.append("selection")
        return {"type": "candidate_selection_ready", "payload": {"options": ["candidate"]}}, Path("display")

    def submit(_pty, answer, *, label):
        calls.append(answer)
        state["execution"]["pending_ask_user_question_input"]["answer"] = {"free_text": answer}
        meta.write_text(yaml.safe_dump(state), encoding="utf-8")

    monkeypatch.setattr(runner, "_wait_repl_display_event", wait_display)
    monkeypatch.setattr(runner, "_repl_wait_ask", lambda *_args, **_kwargs: calls.append("prompt ready"))
    monkeypatch.setattr(runner, "_repl_submit_line_input", submit)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    monkeypatch.setattr(
        runner, "_answer_runtime_question", lambda *_args, **_kw: "reuse first existing VPC; only create SG"
    )
    monkeypatch.setattr(
        runner, "_repl_submit_question_answer",
        lambda _pty, _runtime, text, _pending, *, label: runner._repl_submit_line_input(_pty, text, label=label),
    )
    runner._repl_wait_selection(pty, runtime, clarification_answer="reuse first existing VPC; only create SG")

    assert calls == ["pending question", "prompt ready", "reuse first existing VPC; only create SG", "selection"]
    assert runtime.repl_candidate_wait_count == 2
    assert runtime.diagnostics["repl_step1_clarification_asks"] == 1


def test_repl_post_rollback_confirmation_preserves_stall_failure_without_restart(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    event = {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
        "kind": "deployment_confirmation",
    }}

    class Pty:
        def terminate(self, *, force):
            calls.append(("terminate", force))

        def spawn(self, *, extra_args):
            calls.append(("spawn", extra_args))

        def drain_output(self) -> None:
            pass

    def wait(_runtime, **kwargs):
        calls.append(("wait", kwargs["timeout"], kwargs["occurrence"]))
        if len([item for item in calls if item[0] == "wait"]) == 1:
            runtime.watchdog = {"state": "no_output", "action": "early_abort"}
            raise TimeoutError("stalled")
        return event, Path("display")

    runtime = argparse.Namespace(spec=argparse.Namespace(profile="rollback"),
        args=argparse.Namespace(stream_timeout=900.0, cleanup_vpc_id="vpc-test"),
        checks={"REPL display user_input_required occurrence 1 observed": False},
        diagnostics={},
        watchdog=None,
    )
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: [{
        "type": "candidate_selection_submitted",
    }])
    monkeypatch.setattr(runner, "_wait_repl_display_event", wait)
    monkeypatch.setattr(runner, "_repl_active_deploy_step", lambda _runtime: False)
    monkeypatch.setattr(
        runner, "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    with pytest.raises(TimeoutError, match="stalled"):
        runner._repl_wait_confirmation_after_optional_parameter_asks(Pty(), runtime)

    assert calls == [("wait", 900.0, 1)]
    assert runtime.diagnostics == {}
    assert runtime.watchdog["action"] == "early_abort"
    assert runtime.checks == {"REPL display user_input_required occurrence 1 observed": False}


def test_normal_resume_follows_new_durable_selection_without_waiting_for_timeout(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    display = tmp_path / "projects/project/session/pipeline/display.jsonl"
    display.parent.mkdir(parents=True)
    events = [
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selection_submitted", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selected", "step_id": runner.NEW_STEPS[0]},
        {"type": "user_input_received", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_completed", "step_id": runner.NEW_STEPS[0]},
        {"type": "user_input_required", "step_id": runner.NEW_STEPS[0],
         "payload": {"kind": "candidate_selection"}},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
    ]
    display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    runtime = SimpleNamespace(spec=SimpleNamespace(profile="normal_resume"),
        args=SimpleNamespace(stream_timeout=0.05),
        paths=SimpleNamespace(config_dir=tmp_path, run_dir=tmp_path), checks={}, diagnostics={},
        repl_candidate_wait_count=1, repl_confirmation_wait_count=0,
        repl_confirmation_action_count=0, watchdog=None)
    pty = SimpleNamespace(events=[], transcript="", drain_output=lambda: None)

    def select(_pty, **_kwargs):
        calls.append("submit")
        events.extend([
            {"type": "candidate_selection_submitted", "step_id": runner.NEW_STEPS[0]},
            {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
            {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
                "kind": "deployment_confirmation", "options": [{"action": "confirm"}, {"action": "cancel"}],
            }},
        ])
        display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))
    monkeypatch.setattr(runner, "_repl_select_current", select)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    runner._repl_wait_normal_resume_confirmation(pty, runtime)

    assert calls == ["selection", "submit"]
    assert runtime.repl_confirmation_wait_count == 1
    assert runtime.checks == {}  # A real confirmation was observed, with no swallowed failed wait.
    assert runtime.diagnostics["repl_supplemental_reselections"] == 1


def test_interrupt_rollback_initial_confirmation_follows_native_reselection(runner, tmp_path, monkeypatch):
    """Reproduce the accepted selection followed by a fresh ready event in run 77623104."""
    display = tmp_path / "projects/project/session/pipeline/display.jsonl"
    display.parent.mkdir(parents=True)
    events = [
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selection_submitted", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selected", "step_id": runner.NEW_STEPS[0]},
        {"type": "user_input_received", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_completed", "step_id": runner.NEW_STEPS[0]},
        {"type": "user_input_required", "step_id": runner.NEW_STEPS[0],
         "payload": {"kind": "candidate_selection"}},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
    ]
    display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    runtime = SimpleNamespace(spec=SimpleNamespace(profile="interrupt_rollback"),
        args=SimpleNamespace(stream_timeout=0.05),
        paths=SimpleNamespace(config_dir=tmp_path, run_dir=tmp_path), checks={}, diagnostics={},
        repl_candidate_wait_count=1, repl_confirmation_wait_count=0,
        repl_confirmation_action_count=0, watchdog=None)
    pty = SimpleNamespace(events=[], transcript="", drain_output=lambda: None)
    selections = []
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_: None)
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_: None)
    def select(*_, **__):
        selections.append(True)
        if len(selections) == 2:
            events.extend([
                {"type": "candidate_selection_submitted", "step_id": runner.NEW_STEPS[0]},
                {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
                {"type": "user_input_required", "step_id": runner.NEW_STEPS[1], "payload": {
                    "kind": "deployment_confirmation", "options": [{"action": "confirm"}, {"action": "cancel"}],
                }},
            ])
            display.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    monkeypatch.setattr(runner, "_repl_select_current", select)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    class FirstRollbackReachedError(Exception):
        pass
    def rollback(_runtime, _pty, text):
        assert "只创建安全组" in text and "不创建 VPC 或 VSwitch" in text
        assert runtime.repl_confirmation_wait_count == 1
        raise FirstRollbackReachedError
    monkeypatch.setattr(runner, "_repl_choose_direct_input", rollback)
    with pytest.raises(FirstRollbackReachedError):
        runner._run_repl_interrupt_rollback(runtime, pty)
    assert len(selections) == 2
    assert runtime.diagnostics["repl_supplemental_reselections"] == 1
    assert runtime.checks == {}


def test_interrupt_rollback_keeps_both_fault_inputs_and_real_deployment_checkpoint(runner, monkeypatch):
    calls = []
    runtime = SimpleNamespace(checks={}, cidr="10.250.1.0/24")
    pty = SimpleNamespace(send=lambda text, **kw: calls.append(("confirm", text, kw["label"])))
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_: calls.append("initial"))
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_, **__: calls.append("selection"))
    monkeypatch.setattr(runner, "_repl_select_current", lambda *_: calls.append("select"))
    monkeypatch.setattr(runner, "_repl_wait_confirmation_after_optional_parameter_asks",
                        lambda *_: calls.append("confirmation"))
    monkeypatch.setattr(runner, "_repl_choose_direct_input", lambda _, __, text: calls.append(("input", text)))
    monkeypatch.setattr(runner, "_repl_wait_selection_after_rollback", lambda *_: calls.append("rollback selection"))
    def checkpoint(_pty, _runtime, **kw):
        assert kw["step_id"] == runner.NEW_STEPS[2]
        assert kw["tool_names"] == {"ros_deploy"}
        calls.append("deployment checkpoint")
    monkeypatch.setattr(runner, "_wait_repl_transcript_tool_use", checkpoint)
    monkeypatch.setattr(runner, "_repl_submit_pipeline_interrupt",
                        lambda _, __, text: calls.append(("interrupt", text)))
    runner._run_repl_interrupt_rollback(runtime, pty)
    assert calls == ["initial", "selection", "select", "confirmation",
        ("input", "我改需求了：只创建安全组，不创建 VPC 或 VSwitch；请重新规划。"),
        "rollback selection", "select", "confirmation", ("confirm", "\r", "confirmation-confirm"),
        "deployment checkpoint", ("interrupt", "架构再次变化：改为只创建一个空 VPC，不创建安全组；请重新规划。"),
        "selection", "select", "confirmation", ("input", "取消，不再部署。")]
    assert runtime.checks == {"REPL Step 2 and Step 3 rollback inputs submitted": True}


def test_repl_image_lifecycle_requires_initial_vpc_question(runner: ModuleType) -> None:
    required = {"initial", "selection", "confirmation-adjust", "rollback-interrupt", "normal-followup"}

    assert runner._multimodal_image_lifecycle_complete(required | {"ask-first-answer"})
    assert not runner._multimodal_image_lifecycle_complete(required)
    assert not runner._multimodal_image_lifecycle_complete(required | {"rollback-ask-answer"})
    assert not runner._multimodal_image_lifecycle_complete((required - {"selection"}) | {"ask-first-answer"})


def test_repl_multimodal_confirmation_answers_repeated_asks_before_confirmation(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    def question(tool_id):
        return {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
                'payload': {'kind': 'ask_user_question', 'tool_use_id': tool_id}}
    native_inputs = iter([question('q1'), question('q2'), {
        'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
        'payload': {'kind': 'deployment_confirmation'},
    }])
    monkeypatch.setattr(runner, '_read_repl_display_events', lambda *a: [])
    def wait_native(_runtime, **kwargs):
        assert kwargs['timeout'] == 9.0
        assert kwargs['predicate'] is runner._is_repl_deployment_confirmation
        calls.append(('native_wait', kwargs['occurrence']))
        return next(native_inputs), Path('meta')
    monkeypatch.setattr(runner, '_wait_repl_display_event', wait_native)
    monkeypatch.setattr(runner, '_repl_wait_question_acknowledgement',
                        lambda *a, **kw: calls.append(('acknowledged', kw['label'])))

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", description, timeout, patterns))
            return runner.REPL_ASK_INPUT_READY_PATTERNS[0]

        def drain_output(self) -> None:
            calls.append("drain")

        def paste_image_fixture(self, key: str, **kwargs) -> None:
            calls.append(("fixture", key))

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_repl_paste_generated_image",
        lambda _runtime, _pty, key, text, **kwargs: calls.append(("generated", key, text)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    runner._repl_wait_multimodal_confirmation(
        runtime,
        Pty(),
        primary_image_key="ask-first-answer",
        phase="initial",
    )

    assert calls[0] == ('native_wait', 1)
    assert (
        "expect",
        "initial image ask #1 input ready",
        9.0,
        runner.REPL_ASK_INPUT_READY_PATTERNS,
    ) in calls
    assert ("fixture", "ask-first-answer") in calls
    generated = next(item for item in calls if isinstance(item, tuple) and item[0] == "generated")
    assert generated[1] == "initial-parameter-2"
    assert "首个已有 VPC" in generated[2]
    assert ("send", "\r", "initial-image-ask-enter-1") in calls
    assert ("send", "\r", "initial-image-ask-enter-2") in calls
    assert calls[calls.index(("send", "\r", "initial-image-ask-enter-2")) - 1] == "drain"
    assert calls[-2] == ('native_wait', 1)
    assert calls[-1] == ("confirmation", False)


def test_repl_multimodal_confirmation_uses_phase_specific_generated_answer(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    native_inputs = iter([{
        'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
        'payload': {'kind': 'ask_user_question', 'tool_use_id': 'rollback-question'},
    }, {
        'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
        'payload': {'kind': 'deployment_confirmation'},
    }])
    monkeypatch.setattr(runner, '_read_repl_display_events', lambda *a: [])
    monkeypatch.setattr(runner, '_wait_repl_display_event', lambda *a, **kw: (next(native_inputs), Path('meta')))
    monkeypatch.setattr(runner, '_repl_wait_question_acknowledgement',
                        lambda *a, **kw: calls.append(('acknowledged', kw['label'])))

    class Pty:
        def expect_any(self, _patterns, *, description, timeout):
            calls.append(("expect", description, timeout))
            return runner.REPL_ASK_INPUT_READY_PATTERNS[0]

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_repl_submit_generated_image",
        lambda _runtime, _pty, key, text, *, label, **kwargs: calls.append(("generated", key, text, label)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_wait_confirmation",
        lambda _pty, _runtime, *, require_input_ready: calls.append(("confirmation", require_input_ready)),
    )

    runner._repl_wait_multimodal_confirmation(
        runtime,
        Pty(),
        primary_image_key="rollback-ask-answer",
        primary_image_text="选择第一个已有 VPC，继续创建安全组，不创建 VSwitch。",
        phase="rollback",
    )

    assert (
        "generated",
        "rollback-ask-answer",
        "选择第一个已有 VPC，继续创建安全组，不创建 VSwitch。",
        "rollback-image-ask-enter-1",
    ) in calls
    assert calls[-1] == ("confirmation", False)


def test_repl_multimodal_selection_answers_step1_ask_before_candidates(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(runner, '_pending_repl_parameter_question', lambda *a, **kw: ('pending', 'meta'))
    monkeypatch.setattr(runner, '_repl_wait_question_acknowledgement',
                        lambda *a, **kw: calls.append(('acknowledged', kw['label'])))
    display_events = iter([[], [], [{"type": "candidate_selection_ready"}]])

    class Pty:
        def __init__(self) -> None:
            self.transcript = ""
            self.events: list[dict[str, object]] = []
            self.args = argparse.Namespace(permission_prompt_response="pageup-enter")
            self.drain_count = 0

        def drain_output(self) -> None:
            calls.append("drain")
            self.drain_count += 1
            if self.drain_count == 1:
                self.transcript += "Yes, allow once"
            elif self.drain_count == 2:
                self.transcript += "  > \x1b"

        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0), repl_candidate_wait_count=0)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(
            PERMISSION_PROMPT_PATTERNS=(r"Yes, allow once",),
            _permission_prompt_response_sequence=lambda value: f"allow:{value}",
        ),
    )
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: next(display_events))
    monkeypatch.setattr(
        runner,
        "_repl_submit_generated_image",
        lambda _runtime, _pty, key, text, *, label, **kwargs: calls.append(("generated", key, text, label)),
    )
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args, **_kwargs: calls.append("selection"))

    runner._repl_wait_multimodal_selection(runtime, Pty(), phase="rollback")

    assert ("send", "allow:pageup-enter", "permission-prompt-response") in calls
    generated = next(item for item in calls if isinstance(item, tuple) and item[0] == "generated")
    assert generated[1] == "rollback-step1-answer-1"
    assert "继续规划安全组" in generated[2]
    assert generated[3] == "rollback-step1-image-ask-enter-1"
    assert calls[-1] == "selection"


def test_repl_multimodal_handoff_waits_for_normal_prompt_before_image_followup(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    generated_images: dict[str, str] = {}

    class Pty:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def expect_any(self, patterns, *, description, timeout):
            calls.append(("expect", description, timeout))
            return patterns[0]

    pty = Pty()
    runtime = argparse.Namespace(
        args=argparse.Namespace(stream_timeout=9.0),
        cidr="10.250.0.0/24",
        checks={},
    )

    def submit_image(_pty, key: str, *, label: str) -> None:
        calls.append(("image", key, label))
        pty.events.append({"type": "paste-image-fixture", "image_key": key})

    def wait_multimodal(
        _runtime,
        _pty,
        *,
        primary_image_key: str,
        phase: str,
        primary_image_text: str | None = None,
    ) -> None:
        calls.append(("confirmation", phase, primary_image_key, primary_image_text))
        pty.events.append({"type": "paste-image-fixture", "image_key": primary_image_key})

    def direct_image(_runtime, _pty, key: str, text: str) -> None:
        calls.append(("direct-image", key, text))
        pty.events.append({"type": "paste-image-fixture", "image_key": key})

    def submit_generated_image(_runtime, _pty, key: str, text: str, *, label: str) -> None:
        generated_images[key] = text
        submit_image(_pty, key, label=label)

    monkeypatch.setattr(runner, "_repl_submit_image_fixture", submit_image)
    monkeypatch.setattr(runner, "_repl_submit_generated_image", submit_generated_image)
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args: calls.append("selection"))
    monkeypatch.setattr(
        runner,
        "_repl_wait_multimodal_selection",
        lambda _runtime, _pty, *, phase: calls.append(("multimodal-selection", phase)),
    )
    monkeypatch.setattr(runner, "_repl_wait_multimodal_confirmation", wait_multimodal)
    monkeypatch.setattr(runner, "_repl_choose_direct_image", direct_image)
    monkeypatch.setattr(runner, "_repl_choose_direct_input", lambda *_args: calls.append("cancel"))
    monkeypatch.setattr(
        runner,
        "_legacy_repl_module",
        lambda: argparse.Namespace(_expect_initial_prompt=lambda *_args: calls.append("normal-prompt-ready")),
    )
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())

    runner._run_repl_multimodal_lifecycle(runtime, pty)

    initial_confirmation = next(
        item
        for item in calls
        if isinstance(item, tuple) and item[:3] == ("confirmation", "initial", "ask-first-answer")
    )
    assert "第一个已有 VPC" in initial_confirmation[3]
    assert "不要再次询问" in initial_confirmation[3]
    assert all(marker in generated_images["selection"] for marker in ("VpcId", "问我选哪一个", "不要自行选择"))
    for key in ("initial", "selection"):
        assert "allow_free_text=true" in generated_images[key]
        assert "图片" in generated_images[key]
    handoff_index = calls.index(("expect", "multimodal pipeline handoff", 9.0))
    ready_index = calls.index("normal-prompt-ready")
    followup_index = calls.index(("image", "normal-followup", "normal-followup-image-enter"))
    response_index = calls.index(("expect", "normal image follow-up response", 9.0))
    assert handoff_index < ready_index < followup_index < response_index
    assert runtime.checks["REPL full image lifecycle exercised"] is True


def test_repl_step1_clarification_uses_repl_and_display_event_order(runner: ModuleType) -> None:
    repl_events = [
        {"type": "expect", "description": "pipeline question input ready"},
        {"type": "display-event", "event_type": "candidate_selection_ready"},
        {"type": "candidate-interrupt"},
        {"type": "display-event", "event_type": "candidate_selection_ready"},
    ]
    display_events = [
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_diagram", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_detail", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_diagram", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_detail", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
    ]

    assert runner._repl_step1_clarification_checks(repl_events, display_events) == (True, True)


def test_repl_step1_replan_uses_parameter_free_vpc_target(runner: ModuleType) -> None:
    prompt = runner._repl_step1_replan_prompt(argparse.Namespace(cidr="10.250.9.0/24"))

    assert "只创建一个空 VPC" in prompt
    assert "10.250.9.0/24" in prompt
    assert "不创建 VSwitch、安全组、ECS 或公网资源" in prompt


def test_repl_step1_clarification_answer_is_complete_enough_for_candidates(runner: ModuleType) -> None:
    answer = runner._repl_step1_clarification_answer(argparse.Namespace(cidr="10.250.9.0/24"))

    assert all(item in answer for item in ("杭州", "VPC", "VSwitch", "安全组", "ECS", "10.250.9.0/24"))
    assert all(item in answer for item in ("可用区", "实例规格", "公共镜像", "自动选择"))


def test_repl_step1_clarification_uses_initial_goal_then_changed_goal(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        spec=runner.SCENARIO_BY_NAME["repl-step1-clarify-replan"],
        cidr="10.250.9.0/24", paths=SimpleNamespace(config_dir=tmp_path),
        diagnostics={}, args=SimpleNamespace(cleanup_vpc_id="", cleanup_zone_id=""),
    )
    goals = []

    class PlanningObservedError(Exception):
        pass

    def answer(_config, _pending, facts, *_args, **_kwargs):
        goals.append(facts["goal"])
        return facts["goal"], "purpose"

    def wait(_pty, actual_runtime, **kwargs):
        runner._answer_runtime_question(
            actual_runtime, {"_step_id": runner.NEW_STEPS[0]},
            goal_override=kwargs.get("clarification_answer", ""),
        )
        if len(goals) == 2:
            raise PlanningObservedError

    def interrupt(_pty, actual_runtime, text):
        actual_runtime.current_goal = text

    monkeypatch.setattr(runner, "answer_question", answer)
    monkeypatch.setattr(runner, "question_conversation", lambda _runtime: [])
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: None)
    monkeypatch.setattr(runner, "_repl_wait_selection", wait)
    monkeypatch.setattr(runner, "_repl_submit_candidate_interrupt", interrupt)

    with pytest.raises(PlanningObservedError):
        runner._repl_basic_flow(runtime, object())

    assert goals == [runner._repl_step1_clarification_answer(runtime), runner._repl_step1_replan_prompt(runtime)]
    assert all(item in goals[0] for item in ("VSwitch", "安全组", "1 台无公网 ECS", runtime.cidr))
    assert "只创建一个空 VPC" in goals[1]


@pytest.mark.parametrize(
    ("steps", "require_all", "expected"),
    [
        ((0, 1), False, True),
        ((0, 1), True, False),
        ((0, 1, 2), True, True),
        ((0, 1, 0, 1, 2), True, True),
        ((0, 2), False, False),
        ((1,), False, False),
    ],
)
def test_repl_progress_follows_three_step_state_machine(
    runner: ModuleType, steps: tuple[int, ...], require_all: bool, expected: bool
) -> None:
    display_events = [{"type": "step_started", "step_id": runner.NEW_STEPS[index]} for index in steps]

    assert runner._repl_progress_follows_step_order(display_events, require_all=require_all) is expected


def test_repl_natural_adjustment_is_proven_by_outcomes_without_structured_action(runner: ModuleType) -> None:
    display_events = [
        {
            "type": "user_input_required",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"solution_summary": "before", "effective_deployment_parameters": {"Cidr": "old"}},
        },
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"selected_value": "调整网段并重新询价", "structured": False},
        },
        {
            "type": "user_input_required",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"solution_summary": "after", "effective_deployment_parameters": {"Cidr": "new"}},
        },
        {
            "type": "user_input_received",
            "step_id": runner.NEW_STEPS[1],
            "payload": {"selected_value": "确认部署", "structured": False},
        },
        {"type": "step_started", "step_id": runner.NEW_STEPS[2]},
    ]
    transcript_values = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "preview-1", "name": "ros_preview_template"},
        {"type": "tool_use", "id": "quote-1", "name": "ros_estimate_template_cost"},
        {"type": "tool_use", "id": "preview-2", "name": "ros_preview_template"},
        {"type": "tool_use", "id": "quote-2", "name": "ros_estimate_template_cost"},
    ]}]

    assert runner._repl_natural_adjustment_checks(display_events, transcript_values) == {
        "REPL direct text produced an adjustment": True,
        "REPL natural language confirmation was classified": True,
        "REPL adjustment produced a refreshed confirmation": True,
        "REPL adjustment reran Preview and quote": True,
    }


def test_repl_natural_adjustment_uses_distinct_reserved_subnet(runner: ModuleType) -> None:
    runtime = argparse.Namespace(cidr="10.250.0.0/24")

    assert runner._repl_natural_adjusted_cidr(runtime) == "10.250.0.128/25"


def test_public_journal_tool_names_reads_only_translated_tool_envelopes(runner: ModuleType, tmp_path: Path) -> None:
    journal = tmp_path / "projects" / "project" / "session" / "pipeline" / "a2a-events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps({"events": [
            {"eventType": "tool_result", "data": {"toolName": "aliyun_api", "result": "private"}},
            {"eventType": "text_delta", "data": {"toolName": "private"}},
            {"eventType": "tool_started", "data": {"toolName": "ros_deploy"}},
        ]}) + "\n",
        encoding="utf-8",
    )

    assert runner._public_journal_tool_names(tmp_path) == ["aliyun_api", "ros_deploy"]


def test_public_a2a_tool_use_ids_ignores_non_tool_payloads(runner: ModuleType) -> None:
    attributed = {"metadata": {"iac_code": {"pipeline": {
        "eventType": "tool_result", "data": {"toolName": "aliyun_api", "toolUseId": "call-1"},
    }}}}
    text_only = {"metadata": {"iac_code": {"pipeline": {
        "eventType": "text_delta", "data": {"toolUseId": "private"},
    }}}}

    assert runner._public_a2a_tool_use_ids([attributed, text_only]) == {"call-1"}


def test_public_a2a_attribution_ignores_artifact_reference_but_checks_tool_event(runner: ModuleType) -> None:
    def envelope(event_type: str, tool_name: str | None = None):
        data = {"toolUseId": "call-1"}
        if tool_name is not None:
            data["toolName"] = tool_name
        return {"metadata": {"iac_code": {"pipeline": {"eventType": event_type, "data": data}}}}

    artifact = envelope("artifact_created")
    public_tool = envelope("tool_result", "aliyun_api")
    misattributed_tool = envelope("tool_result", "ros_deploy")

    assert runner._public_a2a_tool_use_ids([artifact]) == {"call-1"}
    assert runner._public_a2a_tool_events_for_id([artifact], "call-1") == []
    assert runner._public_a2a_tool_events_for_id([artifact, public_tool], "call-1") == [
        {"toolUseId": "call-1", "toolName": "aliyun_api"},
    ]
    assert runner._public_a2a_tool_events_for_id([misattributed_tool], "call-1") == [
        {"toolUseId": "call-1", "toolName": "ros_deploy"},
    ]
    assert not runner._public_aliyun_attribution_consistent(
        runner._public_a2a_tool_events_for_id([artifact], "call-1")
    )
    assert runner._public_aliyun_attribution_consistent(
        runner._public_a2a_tool_events_for_id([artifact, public_tool], "call-1")
    )
    assert not runner._public_aliyun_attribution_consistent(
        runner._public_a2a_tool_events_for_id([misattributed_tool], "call-1")
    )
    delegated_tool = envelope("tool_result", "ros_preview_template")
    delegated_events = runner._public_a2a_tool_events_for_id([delegated_tool], "call-1")
    assert runner._public_aliyun_attribution_consistent(delegated_events, "ros_preview_template")
    assert not runner._public_aliyun_attribution_consistent(delegated_events, "ros_validate_template")
    assert runner._public_tool_name_category("aliyun_api") == "aliyun_api_alias"
    assert runner._public_tool_name_category("ros_preview_template") == "other_tool_name"
    assert runner._public_tool_name_category(None) == "missing"


@pytest.mark.parametrize(
    "public_name,event_types,passed",
    [
        ("ros_validate_template", ("tool_started", "tool_result"), True),
        ("aliyun_api", ("tool_started", "tool_result"), False),
        (None, ("tool_started", "tool_result"), False),
        ("ros_validate_template", (), False),
        ("ros_validate_template", ("artifact_created",), False),
    ],
)
def test_public_contract_audit_preserves_actual_delegated_tool_identity(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    public_name: str | None, event_types: tuple[str, ...], passed: bool,
) -> None:
    config_dir = tmp_path / "config"
    transcript = config_dir / "projects" / "project" / "session" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("\n".join(json.dumps(row) for row in [
        {"role": "assistant", "content": [{
            "type": "tool_use", "id": "cloud-call", "name": "ros_validate_template",
        }]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "cloud-call", "content": '{"Parameters": []}',
            "metadata": {"aliyun_http": {
                "contract_version": "aliyun_body_v1", "product": "ros", "version": "2019-09-10",
                "action": "ValidateTemplate", "status": 200, "response_mode": "json", "body_format": "json",
            }},
        }]},
    ]), encoding="utf-8")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=config_dir, run_dir=tmp_path, artifacts_dir=artifacts_dir),
        spec=argparse.Namespace(surface=runner.Surface.A2A, case_id="A02"),
        checks={}, diagnostics={}, notes=[],
    )
    events = [{"metadata": {"iac_code": {"pipeline": {
        "eventType": event_type, "data": {"toolUseId": "cloud-call", "toolName": public_name},
    }}}} for event_type in event_types]
    monkeypatch.setattr(runner, "_all_event_values", lambda _path: events)
    monkeypatch.setattr(runner, "_copied_credential_values", lambda _runtime: [])

    runner.run_public_contract_audit(runtime)

    assert runtime.checks["Aliyun business body and public payload contract passed"] is True
    assert runtime.checks["public events preserve Aliyun tool attribution"] is passed


def test_repl_question_waits_for_actual_input_prompt(runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            calls.append(("question", (patterns, description, timeout)))

        def drain_output(self) -> None:
            calls.append(("drain", None))

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0, timeout=4.0))
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_wait_ask(Pty(), runtime, description="Step 1 question")

    assert calls == [
        ("question", (runner.REPL_ASK_INPUT_READY_PATTERNS, "Step 1 question input ready", 9.0)),
        ("sleep", 0.25),
        ("drain", None),
    ]
    pattern = runner.REPL_ASK_INPUT_READY_PATTERNS[0]
    assert re.search(pattern, "\x1b[0m  > \x1b[?25h")
    assert not re.search(pattern, "> quoted model text")


def test_repl_step2_question_fails_fast_if_confirmation_appears_first(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Pty:
        def expect_any(self, patterns, *, description, timeout):
            assert patterns == runner.REPL_ASK_INPUT_READY_PATTERNS + runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS
            assert description == "Step 2 VPC parameter question input ready"
            assert timeout == 9.0
            return runner.REPL_CONFIRMATION_INPUT_READY_PATTERNS[0]

        def drain_output(self) -> None:
            raise AssertionError("a rejected confirmation must fail before the handoff drain")

    runtime = argparse.Namespace(args=argparse.Namespace(stream_timeout=9.0))

    with pytest.raises(RuntimeError, match="deployment confirmation appeared before Step 2 VPC parameter question"):
        runner._repl_wait_ask(
            Pty(),
            runtime,
            description="Step 2 VPC parameter question",
            reject_confirmation=True,
        )


def test_repl_candidate_interrupt_waits_for_line_editor_handoff(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    fake_repl = argparse.Namespace(_expect_interrupt_input_ready=lambda *_args, **_kwargs: calls.append("ready"))
    monkeypatch.setattr(runner, "_legacy_repl_module", lambda: fake_repl)
    monkeypatch.setattr(runner, "_python_namespace", lambda _runtime: argparse.Namespace())
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))

    runner._repl_submit_candidate_interrupt(Pty(), argparse.Namespace(), "改成空 VPC")

    assert calls == [
        ("send", "\x1b", "candidate-interrupt"),
        "ready",
        ("sleep", 0.25),
        "drain",
        ("send", "\x1b[200~改成空 VPC\x1b[201~", "candidate-interrupt-input"),
        ("sleep", 0.1),
        "drain",
        ("send", "\r", "candidate-interrupt-enter"),
    ]


def test_repl_step2_parameter_waits_only_after_candidate_selection(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="step2_parameter", cloud_write=False), checks={},
        answered_parameter_fields={"vpc_id", "zone_id"},
    )
    monkeypatch.setattr(runner, "_question_facts", lambda _runtime: calls.append("fixtures"))
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: calls.append("initial"))
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args, **_kwargs: calls.append("selection"))
    monkeypatch.setattr(runner, "_repl_select_current", lambda *_args, **_kwargs: calls.append("select"))
    monkeypatch.setattr(runner, "_repl_wait_confirmation_after_optional_parameter_asks",
                        lambda *_args: calls.append("questions-and-confirmation"))
    monkeypatch.setattr(runner, "_repl_choose_direct_input",
                        lambda _runtime, _pty, text: calls.append(("direct", text)))
    runner._repl_basic_flow(runtime, object())
    assert calls == ["fixtures", "initial", "selection", "select", "questions-and-confirmation",
                     ("direct", "取消本次部署，不创建任何云资源。")]
    assert runtime.checks["both required parameters answered"] is True


def test_step2_parameter_prompt_requires_user_answers_instead_of_api_discovery(runner: ModuleType) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="step2_parameter"),
        stack_name="unused-stack",
        cidr="10.250.1.0/24",
    )

    prompt = runner._initial_prompt(runtime)

    assert "VpcId" in prompt
    assert "ZoneId" in prompt
    assert "user_required" in prompt
    assert "禁止通过 API、默认值或推断自行选择" in prompt
    assert "ask_user_question 逐项" in prompt


def test_repl_replace_invalid_uses_candidate_interrupt_editor(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Pty:
        def send(self, text: str, *, label: str) -> None:
            calls.append(("send", text, label))

        def drain_output(self) -> None:
            calls.append("drain")

    runtime = argparse.Namespace(
        spec=argparse.Namespace(profile="replace_invalid", cloud_write=False),
        args=argparse.Namespace(),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(runner, "_repl_submit_initial_prompt", lambda *_args: calls.append("initial"))
    monkeypatch.setattr(runner, "_repl_wait_selection", lambda *_args, **_kwargs: calls.append("selection"))
    monkeypatch.setattr(
        runner,
        "_repl_submit_candidate_interrupt",
        lambda _pty, _runtime, text: calls.append(("candidate-input", text)),
    )
    monkeypatch.setattr(
        runner,
        "_repl_select_current",
        lambda *_args, **kwargs: calls.append(("select", kwargs["next_candidate"])),
    )
    monkeypatch.setattr(
        runner, "_repl_wait_confirmation_after_optional_parameter_asks", lambda *_args: calls.append("confirmation")
    )
    monkeypatch.setattr(
        runner,
        "_repl_choose_direct_input",
        lambda _runtime, _pty, text: calls.append(("direct", text)),
    )

    runner._repl_basic_flow(runtime, Pty())

    assert calls == [
        "initial",
        "selection",
        ("send", "9", "candidate-invalid"),
        ("sleep", 0.25),
        "drain",
        ("candidate-input", "我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch。"),
        "selection",
        ("select", False),
        "confirmation",
        ("direct", "取消本次部署，不创建任何云资源。"),
    ]


def test_repl_replace_invalid_acceptance_requires_replanned_security_group_candidate(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(
            profile="replace_invalid", surface=runner.Surface.REPL, multimodal=False, cloud_write=False
        ),
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        events_path=tmp_path / "events.jsonl",
        checks={},
    )
    repl_events = [
        {"type": "candidate-invalid"},
        {
            "type": "candidate-interrupt-input",
            "text": "\x1b[200~我改需求了：只创建一个安全组，不创建 VPC 或 VSwitch。\x1b[201~",
        },
    ]
    display_events = [
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_detail", "step_id": runner.NEW_STEPS[0], "payload": {"summary": "VPC"}},
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {
            "type": "candidate_detail",
            "step_id": runner.NEW_STEPS[0],
            "payload": {"summary": "仅创建一个安全组"},
        },
        {"type": "candidate_selection_ready", "step_id": runner.NEW_STEPS[0]},
        {"type": "candidate_selected", "step_id": runner.NEW_STEPS[0]},
    ]
    monkeypatch.setattr(runner, "_all_event_values", lambda _path: [])
    monkeypatch.setattr(runner, "_read_json_lines", lambda _path: repl_events)
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: display_events)

    runner.apply_profile_acceptance(runtime)

    assert runtime.checks == {
        "REPL invalid candidate preceded replacement intent": True,
        "REPL replacement reran Step 1 and produced selectable candidates": True,
        "REPL replacement candidate reflects the new security-group target": True,
        "REPL progress follows three-step state machine": True,
    }


def test_repl_step2_parameter_acceptance_requires_both_questions_after_selection(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = argparse.Namespace(
        spec=argparse.Namespace(
            profile="step2_parameter", surface=runner.Surface.REPL, multimodal=False, cloud_write=False
        ),
        paths=argparse.Namespace(run_dir=tmp_path, artifacts_dir=tmp_path / "artifacts"),
        events_path=tmp_path / "events.jsonl",
        checks={},
    )
    repl_events = [
        {"type": "candidate-enter"},
        {"type": "expect", "description": "Step 2 VPC parameter question input ready"},
        {"type": "expect", "description": "Step 2 zone parameter question input ready"},
    ]
    display_events = [
        {"type": "step_started", "step_id": runner.NEW_STEPS[0]},
        {"type": "step_started", "step_id": runner.NEW_STEPS[1]},
    ]
    monkeypatch.setattr(runner, "_all_event_values", lambda _path: [])
    monkeypatch.setattr(runner, "_read_json_lines", lambda _path: repl_events)
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _runtime: display_events)

    runner.apply_profile_acceptance(runtime)

    assert runtime.checks == {
        "deployment parameters were requested only after Step 2 started": True,
        "REPL progress follows three-step state machine": True,
    }


def test_repl_initial_input_is_retried_until_history_acknowledges_it(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    text = "请创建测试网络"
    runtime = argparse.Namespace(
        paths=argparse.Namespace(config_dir=config_dir),
        spec=argparse.Namespace(),
    )
    events: list[dict[str, object]] = []

    class Pty:
        def __init__(self) -> None:
            self.submissions = 0
            self.events = events
            self.pending_text = ""

        def send(self, submitted: str, *, label: str) -> None:
            if label.startswith("initial-input-paste-"):
                attempt = int(label.rsplit("-", 1)[1])
                assert attempt == self.submissions + 1
                assert submitted == f"\x1b[200~{text}\x1b[201~"
                self.pending_text = text
                return
            self.submissions += 1
            assert label == f"initial-input-enter-{self.submissions}"
            assert submitted == "\r"
            if self.submissions == 2 and self.pending_text:
                (config_dir / ".input_history").write_text(
                    json.dumps({"format": "iac-code-input-history-v1", "text": text}) + "\n",
                    encoding="utf-8",
                )

        def drain_output(self) -> None:
            return None

    pty = Pty()
    clock = {"value": 0.0}

    def monotonic() -> float:
        clock["value"] += 1.0
        return clock["value"]

    monkeypatch.setattr(runner, "_initial_prompt", lambda _runtime: text)
    monkeypatch.setattr(runner.time, "monotonic", monotonic)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    runner._repl_submit_initial_prompt(pty, runtime)

    assert pty.submissions == 2
    assert events[0]["type"] == "initial-input-accepted"
    assert events[0]["attempt"] == 2


def test_fault_checkpoint_requires_validation_result_not_tool_start_or_documentation(runner):
    predicate = runner._event_contains('validate', 'template')
    assert not predicate({'pipeline': {'eventType': 'tool_started', 'data': {
        'toolName': 'ros_validate_template'}}}, None)
    assert not predicate({'pipeline': {'eventType': 'tool_result', 'data': {
        'toolName': 'read_file', 'result': 'validate template; CreateStack StackId input_received'}}}, None)
    assert predicate({'pipeline': {'eventType': 'tool_result', 'data': {
        'toolName': 'ros_validate_template', 'isError': False, 'result': {'valid': True}}}}, None)
    assert not predicate({'pipeline': {'eventType': 'tool_result', 'data': {
        'toolName': 'ros_validate_template', 'isError': True, 'result': {'valid': False}}}}, None)
    assert not predicate({'pipeline': {'eventType': 'tool_result', 'data': {
        'toolName': 'ros_validate_template', 'isError': False, 'result': '{"is_success":false}'}}}, None)


def test_create_checkpoint_requires_accepted_resource_event(runner):
    predicate = runner._event_contains('CreateStack', 'StackId')
    assert not predicate({'pipeline': {'eventType': 'tool_result', 'data': {
        'toolName': 'read_file', 'result': {'Action': 'CreateStack', 'StackId': 'example-stack-id'}}}}, None)
    assert predicate({'pipeline': {'eventType': 'stack_current_changed', 'data': {
        'action': 'CreateStack', 'stackId': 'accepted-stack-id', 'isSuccess': True}}}, None)


@pytest.mark.parametrize(('question', 'answer', 'free_text', 'expected'), [
    ('Which ZoneId?', 'cn-hangzhou-test-zone', True, 'cn-hangzhou-test-zone'),
    ('Which CidrBlock?', '10.0.0.0/24', True, '10.0.0.0/24'),
    ('Choose a VPC', 'second-vpc-option', False, '2'),
])
def test_restored_waiting_answer_uses_actual_pending_question(
    runner, monkeypatch, tmp_path, question, answer, free_text, expected,
):
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=600, cleanup_vpc_id='vpc-fixed-old-answer'),
                              checks={})
    pty = SimpleNamespace(events=[{'type': 'spawn', 'command': ['--continue']}] * 4)
    initial = {'step_id': runner.NEW_STEPS[0], 'payload': {'question': '用途?', 'allow_free_text': True}}
    restored = {'step_id': runner.NEW_STEPS[1], 'payload': {
        'question': question, 'allow_free_text': free_text,
        'options': [{'id': 'first-vpc-option'}, {'id': 'second-vpc-option'}],
    }}
    sent = []
    questions = []
    monkeypatch.setattr(runner, '_repl_submit_initial_prompt', lambda *_: None)
    monkeypatch.setattr(runner, '_restart_repl_at_waiting', lambda *_: None)
    monkeypatch.setattr(runner, '_repl_select_current', lambda *_: None)
    monkeypatch.setattr(runner, '_repl_choose_direct_input', lambda *_: None)
    monkeypatch.setattr(runner, '_pending_repl_parameter_question',
                        lambda *_a, **kw: (initial if kw.get('step_id') == runner.NEW_STEPS[0] else restored,
                                          tmp_path / 'meta.yaml'))

    def answer_question(_runtime, payload):
        questions.append(payload['question'])
        return 'network test' if payload is initial['payload'] else answer

    def submit(_pty, _runtime, text, pending, **kwargs):
        sent.append(text)
        _runtime.checks[kwargs['label'] + ' acknowledged'] = True

    monkeypatch.setattr(runner, '_answer_runtime_question', answer_question)
    monkeypatch.setattr(runner, '_repl_submit_question_answer', submit)
    runner._run_repl_waiting_resume_all(runtime, pty)
    assert sent == ['network test', expected]
    assert questions == ['用途?', question]
    assert runtime.checks['restored Step 2 answer acknowledged'] is True
    assert runtime.checks['all four REPL waiting states resumed'] is True


@pytest.mark.parametrize(('receipt_count', 'replacement_after_resume'), [(0, False), (1, False), (2, False), (1, True)])
def test_fault_final_recovery_uses_only_unique_accepted_stack_receipt(
    runner, monkeypatch, tmp_path, receipt_count, replacement_after_resume,
):
    from scripts.ci import stack_ownership

    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path, workspace_dir=tmp_path,
                                                   artifacts_dir=tmp_path), checks={})
    prompts = []
    harness = SimpleNamespace(start_stream=lambda **kw: object(),
                              stream=lambda **kw: prompts.append(kw['prompt']) or object())
    plan = SimpleNamespace(confirmation_answers=['confirm'])
    monkeypatch.setattr(runner, '_initial_prompt', lambda *_: 'initial cloud intent')
    monkeypatch.setattr(runner, '_kill_restart_at', lambda *_a, **_kw: None)
    monkeypatch.setattr(runner, '_continue_a2a_to_pending', lambda *_a, **_kw: None)
    resumed = [False]
    monkeypatch.setattr(runner, '_continue_a2a_from_summary', lambda *_a, **_kw: resumed.__setitem__(0, True))
    monkeypatch.setattr(runner, '_a2a_response_for_pending', lambda *_a: ('candidate', None))
    monkeypatch.setattr(stack_ownership, 'case_pipeline_dirs', lambda config, cwd: [tmp_path])
    monkeypatch.setattr(stack_ownership, 'creation_receipts', lambda dirs: [
        {'stackId': f'accepted-stack-{index}', 'regionId': 'cn-hangzhou'}
        for index in range(receipt_count + (1 if replacement_after_resume and resumed[0] else 0))
    ])
    a2a = SimpleNamespace(_step_started=lambda *_: None)
    if receipt_count != 1:
        with pytest.raises(RuntimeError, match='exactly one accepted original Stack receipt'):
            runner._run_a2a_fault_checkpoints(runtime, harness, a2a, plan)
        assert not any('stack_id=' in prompt for prompt in prompts)
        assert 'all six fault checkpoints exercised' not in runtime.checks
        return
    if replacement_after_resume:
        with pytest.raises(RuntimeError, match='replacement Stack'):
            runner._run_a2a_fault_checkpoints(runtime, harness, a2a, plan)
        assert runtime.checks['fault recovery retained original Stack receipt'] is False
        assert 'all six fault checkpoints exercised' not in runtime.checks
        return
    runner._run_a2a_fault_checkpoints(runtime, harness, a2a, plan)
    assert 'stack_id=accepted-stack-0' in prompts[-1]
    assert 'region_id=cn-hangzhou' in prompts[-1]
    assert '禁止创建第二个 Stack' in prompts[-1]
    assert runtime.checks['fault recovery original Stack receipt verified'] is True
    assert runtime.checks['all six fault checkpoints exercised'] is True


@pytest.mark.parametrize('created', [True, False])
def test_happy_repl_requires_native_requested_subnet_creation(runner, tmp_path, monkeypatch, created):
    spec = next(spec for spec in runner.SCENARIOS if spec.profile == 'happy_single')
    runtime = SimpleNamespace(spec=spec, paths=SimpleNamespace(run_dir=tmp_path, artifacts_dir=tmp_path),
                              events_path=tmp_path / 'events.jsonl', checks={}, cidr='10.22.0.0/24')
    observed = []
    monkeypatch.setattr(runner, '_all_event_values', lambda _: [])
    monkeypatch.setattr(runner, '_read_repl_display_events', lambda _: [])
    def verify(_runtime, *, expected_cidr):
        observed.append(expected_cidr)
        return created
    monkeypatch.setattr(runner, '_verify_requested_vswitch_cidr', verify)
    runner.apply_profile_acceptance(runtime)
    assert runtime.checks['REPL happy path created requested VSwitch'] is created
    assert observed == [runtime.cidr]


def test_resource_discovery_ignores_documentation_and_correlates_real_cloud_tool_results(runner, tmp_path):
    runtime = SimpleNamespace(paths=SimpleNamespace(run_dir=tmp_path, config_dir=tmp_path / 'config',
        artifacts_dir=tmp_path / 'artifacts'), owned_stack_names={'iac-e2e-owned'}, cloud_resources=[])
    (tmp_path / 'artifacts').mkdir()
    rows = [
        {'pipeline': {'eventType': 'tool_result', 'data': {'toolName': 'read_file', 'result': {
            'example': {'Action': 'CreateStack', 'StackId': 'example-stack-id'}}}}},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'real-create', 'name': 'ros_deploy',
            'input': {'action': 'create', 'stack_name': 'iac-e2e-owned', 'region_id': 'cn-hangzhou'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'real-create',
            'content': json.dumps({'stack_id': 'real-stack-id', 'is_success': True})}]},
    ]
    (tmp_path / 'test.events.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in rows), encoding="utf-8")
    resources = runner.discover_cloud_resources(runtime)
    assert len(resources) == 1
    assert resources[0]['stackId'] == 'real-stack-id'
    assert resources[0]['stackName'] == 'iac-e2e-owned'


def test_repl_parameter_completion_cannot_pass_with_only_one_answer(runner, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='step2_parameter', cloud_write=False), checks={},
                              answered_parameter_fields={'vpc_id'})
    for name in ('_question_facts', '_repl_submit_initial_prompt', '_repl_wait_selection',
                 '_repl_select_current', '_repl_wait_confirmation_after_optional_parameter_asks'):
        monkeypatch.setattr(runner, name, lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match='both required parameters'):
        runner._repl_basic_flow(runtime, object())
    assert runtime.checks['both required parameters answered'] is False


def test_required_parameters_must_be_preserved_in_real_confirmation(runner, monkeypatch):
    runtime = SimpleNamespace(checks={}, diagnostics={})
    monkeypatch.setattr(runner, '_question_facts', lambda _: {'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i'})
    with pytest.raises(RuntimeError, match='preserve both'):
        runner._verify_required_parameter_confirmation(runtime, {
            'effective_deployment_parameters': {'VpcId': 'vpc-other', 'ZoneId': 'cn-hangzhou-i'}})
    assert runtime.diagnostics == {'required_parameter_vpc_confirmed': False,
                                   'required_parameter_zone_confirmed': True}
    runner._verify_required_parameter_confirmation(runtime, {
        'effective_deployment_parameters': {'VpcId': 'vpc-fixture', 'ZoneId': 'cn-hangzhou-i'}})
    assert runtime.checks['both required parameter values preserved in confirmation'] is True


def test_goal_override_rebuilds_scope_without_reusing_old_target_clauses(runner, tmp_path, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='rollback'), paths=SimpleNamespace(config_dir=tmp_path),
                              diagnostics={})
    monkeypatch.setattr(runner, '_question_facts', lambda _: {
        'goal': '创建 VSwitch', 'resource_scope': '创建 VSwitch', 'constraints': '部署旧 VSwitch',
        'vpc_id': 'vpc-fixture'})
    facts_seen = []
    def answer(_config, _pending, facts, *_args, **_kwargs):
        facts_seen.append(facts)
        return facts['goal'], 'goal'
    monkeypatch.setattr(runner, 'answer_question', answer)
    runner._answer_runtime_question(runtime, {'question': '新目标?'}, goal_override='只创建安全组，不创建 VSwitch')
    assert facts_seen[0]['resource_scope'] == '只创建安全组，不创建 VSwitch'
    assert '部署旧 VSwitch' not in facts_seen[0]['constraints']
    assert facts_seen[0]['vpc_id'] == 'vpc-fixture'


@pytest.mark.parametrize('profile', ['backup_restore', 'waiting_resume', 'input_during_backup'])
def test_recovery_answers_use_fixture_target_instead_of_vague_initial_question(runner, profile):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile=profile), cidr='192.168.12.0/24', stack_name='iac-e2e-fake')
    facts = runner._question_facts(runtime)
    assert '创建一个 VSwitch' in facts['goal']
    assert 'user_required' in facts['goal']
    assert '不部署' in facts['goal']
    assert '我有个产品要上线' not in facts['goal']
    assert 'vpc_id' not in facts  # Still must exercise the Step 2 question.


def test_rollback_stream_updates_question_goal_before_recovery(runner, monkeypatch):
    runtime = SimpleNamespace(stack_name='iac-e2e-owned', current_goal='创建 VSwitch')
    monkeypatch.setattr(runner, '_advance_a2a_to_pending', lambda *_args, **_kwargs: None)
    plan = SimpleNamespace(confirmation_answers=[])
    class Harness:
        def start_stream(self, **kwargs):
            assert runtime.current_goal == kwargs['prompt']
            assert '只创建一个安全组' in runtime.current_goal
            raise ValueError('boundary verified')
    with pytest.raises(ValueError, match='boundary verified'):
        runner._run_a2a_rollback_recovery(runtime, Harness(), object(), plan, runner.NEW_STEPS[0])


def test_rollback_question_facts_include_existing_vpc_without_changing_new_target(runner, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='rollback_step1'), stack_name='iac-e2e-owned',
                              cidr='192.168.12.0/24', current_goal='只创建安全组，不创建 VSwitch', env={},
                              args=SimpleNamespace(python='python'))
    monkeypatch.setattr(runner, 'network_facts', lambda *_args: {
        'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i', 'cidr': '192.168.12.0/24'})
    facts = runner._question_facts(runtime)
    assert facts['goal'] == runtime.current_goal
    assert facts['vpc_id'] == 'vpc-fixture'
    assert facts['region'] == 'cn-hangzhou'
    assert facts['cloud_vendor'] == '阿里云'
    assert facts['resource_scope'] == '只创建安全组，不创建 VSwitch'
    assert runner._resolve_runtime_question_facts(runtime, ('region',)) == {'region': 'cn-hangzhou'}


def test_network_region_preference_does_not_invent_an_alibaba_fixture(runner, monkeypatch):
    runtime = SimpleNamespace(question_facts={'goal': '只规划 AWS'}, cidr='10.250.1.0/24')
    monkeypatch.setattr(runner, 'network_facts', lambda *_: pytest.fail('must not query a new fixture'))
    assert runner._resolve_runtime_question_facts(runtime, ('region', 'cloud_vendor')) == {}


def test_image_rollback_keeps_declared_location_without_querying_a_fixture(runner, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='image_interrupt'), cidr='10.22.0.0/24',
                              current_goal='只创建安全组，不创建 VPC 或 VSwitch', stack_name='')
    monkeypatch.setattr(runner, 'network_facts', lambda *_: pytest.fail('location needs no cloud lookup'))
    facts = runner._question_facts(runtime)
    assert '杭州' in facts['region'] and '阿里云' in facts['cloud_vendor']
    assert facts['resource_scope'] == runtime.current_goal
    assert 'ECS' not in facts['constraints']


def test_changed_literal_location_overrides_retained_location(runner):
    facts = runner._facts_for_current_goal('只在 cn-beijing 创建阿里云安全组',
                                         {'region': 'cn-hangzhou', 'cloud_vendor': 'AWS'})
    assert 'cn-beijing' in facts['region'] and 'cn-hangzhou' not in facts['region']
    assert '阿里云' in facts['cloud_vendor'] and 'AWS' not in facts['cloud_vendor']


def test_undeclared_location_is_not_created_from_a_new_scope(runner, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='image_interrupt'), cidr='10.22.0.0/24',
                              current_goal='只创建安全组', stack_name='')
    monkeypatch.setattr(runner, '_initial_prompt', lambda _: '规划网络')
    facts = runner._question_facts(runtime)
    assert 'region' not in facts and 'cloud_vendor' not in facts


def test_changed_goal_keeps_fixture_location_but_rebuilds_resource_scope(runner, tmp_path, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='rollback'), paths=SimpleNamespace(config_dir=tmp_path),
                              diagnostics={})
    monkeypatch.setattr(runner, '_question_facts', lambda _: {
        'goal': '创建 VSwitch', 'resource_scope': '创建 VSwitch', 'constraints': '部署旧 VSwitch',
        'vpc_id': 'vpc-fixture', 'region': 'cn-hangzhou', 'cloud_vendor': '阿里云'})
    def answer(_config, _pending, facts, *_args, **_kwargs):
        assert facts['region'] == 'cn-hangzhou'
        assert facts['cloud_vendor'] == '阿里云'
        assert facts['resource_scope'] == '只创建安全组，不创建 VSwitch'
        assert '部署旧 VSwitch' not in facts['constraints']
        return facts['goal'], 'goal'
    monkeypatch.setattr(runner, 'answer_question', answer)
    runner._answer_runtime_question(runtime, {'question': '新目标?'}, goal_override='只创建安全组，不创建 VSwitch')


def test_cleanup_stops_on_delete_failed_instead_of_reissuing_for_fifteen_minutes(
    runner, tmp_path, monkeypatch, capsys,
):
    from iac_code.services import cloud_credentials
    from iac_code.tools.cloud.aliyun import ros_client
    manifest = tmp_path / 'stack.json'
    manifest.write_text(json.dumps({'stackId': 'private-stack', 'stackName': 'iac-e2e-owned'}), encoding='utf-8')
    class Client:
        deletes = 0
        def get_stack(self, _request):
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                'StackName': 'iac-e2e-owned', 'Status': 'DELETE_FAILED' if self.deletes else 'CREATE_COMPLETE'}))
        def delete_stack(self, _request):
            self.deletes += 1
    client = Client()
    monkeypatch.setattr(cloud_credentials, 'CloudCredentials', lambda: SimpleNamespace(
        get_provider=lambda _: SimpleNamespace(region_id='cn-hangzhou')))
    monkeypatch.setattr(ros_client.RosClientFactory, 'create', lambda *_args: client)
    monkeypatch.setattr(sys, 'argv', ['cleanup', str(manifest)])
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='deletion failed after accepted delete'):
        exec(runner._CLOUD_CLEANUP_CODE, {})
    assert client.deletes == 1
    diagnostic = json.loads(capsys.readouterr().out)['cleanupDiagnostic']
    assert diagnostic['status'] == 'DELETE_FAILED'
    assert diagnostic['stage'] == 'get_stack'
    assert 'private-stack' not in json.dumps(diagnostic)


def test_network_question_facts_are_lazy_requested_only_and_cached(runner, monkeypatch):
    runtime = SimpleNamespace(args=SimpleNamespace(python='python'), env={}, cidr='10.250.1.0/24')
    calls = []
    def fetch(*_args):
        calls.append('read-only')
        return {'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i', 'cidr': '10.251.1.0/24'}
    monkeypatch.setattr(runner, 'network_facts', fetch)
    assert runner._resolve_runtime_question_facts(runtime, ('purpose',)) == {}
    assert calls == []
    assert runner._resolve_runtime_question_facts(runtime, ('vpc_id',)) == {'vpc_id': 'vpc-fixture'}
    assert runner._resolve_runtime_question_facts(runtime, ('zone_id',)) == {'zone_id': 'cn-hangzhou-i'}
    assert calls == ['read-only']
    assert runtime.cidr == '10.250.1.0/24'


@pytest.mark.parametrize('profile', ['backup_restore', 'waiting_resume', 'input_during_backup'])
def test_step2_answer_uses_subnet_reserved_for_already_resolved_vpc(runner, tmp_path, monkeypatch, profile):
    runtime = SimpleNamespace(
        spec=SimpleNamespace(profile=profile), paths=SimpleNamespace(config_dir=tmp_path), diagnostics={},
        cidr='10.250.1.0/24', question_facts={'cidr': '10.250.1.0/24', 'cidr_prefix': '24'},
        args=SimpleNamespace(python='python'), env={},
    )
    lookups = []
    def fetch(*_args):
        lookups.append('read-only')
        return {'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i', 'cidr': '192.168.12.0/25'}
    monkeypatch.setattr(runner, 'network_facts', fetch)
    # Resolving a VPC ID reserves a real compatible subnet too, but returns only the requested ID.
    assert runner._resolve_runtime_question_facts(runtime, ('vpc_id',)) == {'vpc_id': 'vpc-fixture'}
    def answer(_config, _pending, facts, *_args, **_kwargs):
        assert facts['cidr'] == '192.168.12.0/25'
        assert facts['cidr_prefix'] == '25'
        assert 'vpc_id' not in facts  # Identities still go through the requested-only resolver.
        assert 'zone_id' not in facts
        return facts['cidr'], 'cidr'
    monkeypatch.setattr(runner, 'answer_question', answer)
    assert runner._answer_runtime_question(runtime, {
        'question': '交换机网段?', '_step_id': runner.NEW_STEPS[1],
    }) == '192.168.12.0/25'
    assert lookups == ['read-only']
    assert runtime.cidr == runtime.question_facts['cidr'] == '192.168.12.0/25'


@pytest.mark.parametrize(('step', 'goal', 'override', 'expected'), [
    (0, '', '', '10.250.1.0/24'),
    (1, '交换机网段必须为 10.250.1.0/24', '', '10.250.1.0/24'),
    (1, '', '交换机网段必须为 10.250.1.0/24', '10.250.1.0/24'),
])
def test_cached_subnet_does_not_replace_step1_or_explicit_goal(runner, tmp_path, monkeypatch,
                                                             step, goal, override, expected):
    runtime = SimpleNamespace(
        spec=SimpleNamespace(profile='input_during_backup'), paths=SimpleNamespace(config_dir=tmp_path),
        diagnostics={}, cidr='10.250.1.0/24', current_goal=goal,
        network_question_facts={'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i', 'cidr': '192.168.12.0/25'},
    )
    monkeypatch.setattr(runner, 'network_facts', lambda *_: pytest.fail('must not query another fixture'))
    def answer(_config, _pending, facts, *_args, **_kwargs):
        assert facts['cidr'] == expected
        assert facts['cidr_prefix'] == '24'
        assert 'vpc_id' not in facts
        return facts['cidr'], 'cidr'
    monkeypatch.setattr(runner, 'answer_question', answer)
    runner._answer_runtime_question(runtime, {'question': '交换机网段?', '_step_id': runner.NEW_STEPS[step]},
                                    goal_override=override)
    assert runtime.cidr == '10.250.1.0/24'


def test_step1_question_wait_records_the_description_required_by_acceptance(runner, tmp_path, monkeypatch):
    runtime = SimpleNamespace(repl_candidate_wait_count=0, args=SimpleNamespace(stream_timeout=1), diagnostics={})
    pty = SimpleNamespace(events=[], transcript='', drain_output=lambda: None)
    question = {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[0],
                'payload': {'kind': 'ask_user_question', 'tool_use_id': 'question-1'}}
    candidate = {'type': 'candidate_selection_ready', 'step_id': runner.NEW_STEPS[0]}
    answers = iter([(question, tmp_path / 'meta.yaml'), (candidate, tmp_path / 'display.jsonl')])
    monkeypatch.setattr(runner, '_wait_repl_display_event', lambda *_args, **_kwargs: next(answers))
    def ready(_pty, _runtime, *, description, **_kwargs):
        pty.events.append({'type': 'expect', 'description': description + ' input ready'})
    monkeypatch.setattr(runner, '_repl_wait_ask', ready)
    monkeypatch.setattr(runner, '_answer_runtime_question', lambda *_args, **_kwargs: '仅规划')
    monkeypatch.setattr(runner, '_repl_submit_question_answer', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    runner._repl_wait_selection(pty, runtime)
    assert runner._repl_step1_clarification_checks(pty.events, [])[0] is True
    # The same prompt after selection must still fail the unchanged ordering check.
    assert runner._repl_step1_clarification_checks(list(reversed(pty.events)), [])[0] is False


def test_backup_directory_does_not_prove_current_checkpoint(runner, tmp_path):
    from iac_code.services.session_backup_state import SessionBackupState
    primary, backup = tmp_path / 'primary', tmp_path / 'backup'
    state = SessionBackupState.bootstrap('session-1', writer_id='writer').committed_next(
        commit_id='commit-1', reason='pipeline_waiting_input', writer_id='writer', proofs={})
    for path in (primary, backup):
        (path / 'pipeline').mkdir(parents=True)
        (path / '.backup-state.json').write_text(json.dumps(state.to_dict()), encoding='utf-8')
        (path / 'pipeline/meta.yaml').write_text('current_step: step1\n', encoding='utf-8')
        (path / 'pipeline/context.yaml').write_text('value: same\n', encoding='utf-8')
    assert runner._backup_checkpoint_is_current(primary, backup, 'session-1') is True
    newer = state.committed_next(commit_id='commit-2', reason='pipeline_waiting_input', writer_id='writer', proofs={})
    (primary / '.backup-state.json').write_text(json.dumps(newer.to_dict()), encoding='utf-8')
    assert runner._backup_checkpoint_is_current(primary, backup, 'session-1') is False
    (backup / '.backup-state.json').write_text(json.dumps(newer.to_dict()), encoding='utf-8')
    (backup / 'pipeline/meta.yaml').write_text('current_step: stale\n', encoding='utf-8')
    assert runner._backup_checkpoint_is_current(primary, backup, 'session-1') is False
    (backup / 'pipeline/meta.yaml').write_text('current_step: step1\n', encoding='utf-8')
    assert runner._backup_checkpoint_is_current(primary, backup, 'session-1') is True
    assert runner._backup_checkpoint_is_current(primary, backup, 'other-session') is False
    (backup / 'pipeline/context.yaml').unlink()
    assert runner._backup_checkpoint_is_current(primary, backup, 'session-1') is False
def test_rollback_cleanup_question_driver_uses_new_goal_at_direct_stream_boundary(runner, monkeypatch, tmp_path):
    runtime = SimpleNamespace(
        paths=SimpleNamespace(config_dir=tmp_path), env={},
        spec=SimpleNamespace(profile='rollback_cleanup_recovery'), stack_name='iac-e2e-fixture',
        owned_stack_names={'iac-e2e-fixture'}, current_goal='原目标创建 VSwitch',
        question_facts={'vpc_id': 'vpc-fixture', 'zone_id': 'cn-hangzhou-i'}, cidr='10.0.1.0/24',
        args=SimpleNamespace(stream_timeout=1),
    )
    class StopAtRollbackError(RuntimeError):
        pass
    class Harness:
        def start_stream(self, *, prompt, name):
            if name == 'cleanup-rollback-new-intent':
                facts = runner._question_facts(runtime)
                assert facts['goal'] == prompt
                assert '不创建 VPC 或 VSwitch' in facts['goal']
                assert 'StackName' not in facts['goal']
                raise StopAtRollbackError
            return SimpleNamespace(wait_for=lambda *_a, **_k: None)
    monkeypatch.setattr(runner, '_advance_a2a_to_pending', lambda *_a, **_k: None)
    plan = SimpleNamespace(confirmation_answers=['confirm'])
    with pytest.raises(StopAtRollbackError):
        runner._run_a2a_rollback_cleanup(runtime, Harness(), SimpleNamespace(), plan, recover_cleanup=True)


def test_confirmation_image_carrier_is_adjustment_not_authorization(runner):
    calls = []
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='image_asks'), event=lambda *a, **kw: None)
    class Harness:
        def stream_image_text(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(context_id='ctx', task_id='task', last_input_required_step_id='')
    text = '将网段改为 192.168.24.0/24，重新 Preview 和询价，不要部署。'
    runner._a2a_turn(runtime, Harness(), prompt=text, name='adjust', image_key='confirmation-adjust')
    assert calls[0]['text'] == text
    assert '192.168.24.0/24' not in calls[0]['prompt']
    assert '不是确认部署' in calls[0]['prompt']
    assert '等待下一轮明确确认' in calls[0]['prompt']


def test_question_facts_expose_authoritative_stack_name_without_cloud_lookup(runner, monkeypatch):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='reselect_progress'), stack_name='iac-e2e-owned',
                              cidr='192.168.24.0/24', current_goal='只规划网络，不部署')
    monkeypatch.setattr(runner, '_initial_prompt', lambda _: runtime.current_goal)
    facts = runner._question_facts(runtime)
    assert facts['stack_name'] == 'iac-e2e-owned'
    assert facts['cidr'] == runtime.cidr
    assert 'vpc_id' not in facts


def test_step_start_wait_rejects_repeated_candidate_without_rescue(runner, monkeypatch, tmp_path):
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=1), diagnostics={})
    pty = SimpleNamespace(events=[])
    ready = {'type': 'candidate_selection_ready', 'step_id': runner.NEW_STEPS[0]}
    monkeypatch.setattr(runner, '_wait_repl_display_event', lambda *a, **kw: (ready, tmp_path))
    with pytest.raises(RuntimeError, match='new candidate selection boundary'):
        runner._repl_wait_step_started(pty, runtime, step_id=runner.NEW_STEPS[1], occurrence=1, description='Step 2')
    assert runtime.diagnostics['repl_unexpected_candidate_before_step2'] is True
    assert not pty.events


def test_step_start_wait_answers_real_clarification_before_observing_start(runner, monkeypatch, tmp_path):
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=1), diagnostics={})
    pty = SimpleNamespace(events=[])
    question = {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[0],
                'payload': {'kind': 'ask_user_question', 'tool_use_id': 'ask-1', 'allow_free_text': True}}
    events = iter([(question, tmp_path), ({'type': 'step_started', 'step_id': runner.NEW_STEPS[1]}, tmp_path)])
    monkeypatch.setattr(runner, '_wait_repl_display_event', lambda *a, **kw: next(events))
    monkeypatch.setattr(runner, '_answer_runtime_question', lambda *_: 'literal supplied facts')
    monkeypatch.setattr(runner, '_repl_wait_ask', lambda *a, **kw: None)
    submitted = []
    monkeypatch.setattr(runner, '_repl_submit_question_answer', lambda *a, **kw: submitted.append(a[2]))
    runner._repl_wait_step_started(pty, runtime, step_id=runner.NEW_STEPS[1], occurrence=1, description='Step 2')
    assert submitted == ['literal supplied facts']
    assert pty.events[0]['event_type'] == 'step_started'


@pytest.mark.parametrize(('types', 'expected'), [
    (['candidate_selection_ready', 'candidate_selection_ready', 'candidate_selection_submitted'], False),
    (['candidate_selection_ready', 'candidate_selection_submitted', 'candidate_selection_ready'], True),
    (['candidate_selection_ready', 'candidate_selection_submitted', 'candidate_selection_ready',
      'candidate_selection_submitted'], False),
    (['candidate_selection_ready'], False),
])
def test_pending_candidate_boundary_uses_order_after_latest_submission(runner, monkeypatch, tmp_path, types, expected):
    runtime = SimpleNamespace(repl_candidate_wait_count=1, paths=SimpleNamespace(run_dir=tmp_path))
    events = [{'type': kind} for kind in types]
    monkeypatch.setattr(runner, '_pending_repl_parameter_question', lambda *_, **__: None)
    monkeypatch.setattr(runner, '_read_repl_display_events', lambda _: events)
    result = runner._pending_repl_input_before_confirmation(runtime, set())
    assert (result is not None) is expected
    if expected:
        assert result[0] is events[-1]


def test_repl_creation_checkpoint_uses_accepted_receipt_without_terminal_stack_text(runner, monkeypatch, tmp_path):
    import scripts.ci.stack_ownership as ownership

    pipeline = tmp_path / 'pipeline'
    pipeline.mkdir()
    (pipeline / 'meta.yaml').write_text(yaml.safe_dump({'attempts': {'items': {
        'attempt-one': {'step_id': 'deploying'}}}}), encoding='utf-8')
    resource = {'provider': 'ros', 'resource_type': 'stack', 'observed_action': 'CreateStack',
                'resource_id': 'stack-created-one', 'resource_name': 'model-chosen-name',
                'region_id': 'cn-hangzhou', 'source_step_id': 'deploying', 'source_attempt_id': 'attempt-one',
                'metadata': {'tool_use_id': 'call-one', 'tool_name': 'ros_deploy'}}
    (pipeline / 'cleanup.yaml').write_text(yaml.safe_dump({'observed_resources': [resource]}), encoding='utf-8')
    monkeypatch.setattr(ownership, 'case_pipeline_dirs', lambda *_: [pipeline])
    monkeypatch.setattr(runner, '_repl_active_deploy_step', lambda _: True)
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path, workspace_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1), checks={})
    pty = SimpleNamespace(transcript='Deploying...', drain_output=lambda: None)
    assert runner._wait_repl_created_stack(runtime, pty, exclude=set()) == 'stack-created-one'
    # Neither an adopted ID nor the successful end of the step is the running crash point.
    resource['observed_action'] = 'WaitStack'
    (pipeline / 'cleanup.yaml').write_text(yaml.safe_dump({'observed_resources': [resource]}), encoding='utf-8')
    monkeypatch.setattr(runner, '_repl_active_deploy_step', lambda _: False)
    with pytest.raises(RuntimeError, match='running step'):
        runner._wait_repl_created_stack(runtime, pty, exclude=set())


@pytest.mark.parametrize('old_deleted', [True, False])
def test_repl_cleanup_recovery_requires_real_cleanup_restart_and_second_deployment(
    runner, monkeypatch, tmp_path, old_deleted,
):
    events = []
    old, new = 'stack-first-create', 'stack-second-create'
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path, workspace_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1), checks={},
                              event=lambda name, **kw: events.append((name, kw)))
    monkeypatch.setattr(runner, '_python_namespace', lambda _: SimpleNamespace())
    for name in ('_repl_submit_initial_prompt', '_repl_select_current', '_repl_wait_confirmation',
                 '_repl_wait_step_started', '_repl_wait_confirmation_after_optional_parameter_asks',
                 '_repl_wait_pipeline_completed'):
        monkeypatch.setattr(runner, name, lambda *a, _name=name, **k: events.append((_name, k)))
    monkeypatch.setattr(runner, '_repl_wait_selection', lambda *a, **k: events.append(('selection', k)))
    created = []
    def first_stack(*a, **k):
        created.append(old)
        return old
    def independent_fixture(*a):
        # A just-created VPC can become visible in DescribeVpcs before its
        # physical ID is visible in the owning Stack's resource inventory.
        # Selecting the independent fixture after CreateStack cannot exclude
        # that VPC reliably. Resolve it before this case creates any resources.
        assert not created, 'independent VPC was selected during the old Stack creation race'
        events.append(('independent_fixture', {}))
        return {'vpc_id': 'vpc-independent-fixture'}
    monkeypatch.setattr(runner, '_wait_repl_created_stack', first_stack)
    monkeypatch.setattr(runner, '_resolve_runtime_question_facts', independent_fixture)
    monkeypatch.setattr(runner, '_repl_submit_pipeline_interrupt',
                        lambda *a: events.append(('interrupt', a[-1])))
    monkeypatch.setattr(runner, 'discover_cloud_resources', lambda _: [
        {'stackId': old, 'createdByCase': 'true'}, {'stackId': new, 'createdByCase': 'true'}])
    actual_legacy = runner._legacy_repl_module()
    cleanup = {'cleanup_status': 'completed' if old_deleted else 'failed',
               'progress_status': 'DELETE_COMPLETE' if old_deleted else 'DELETE_FAILED'}
    legacy = SimpleNamespace(
        _wait_for_cleanup_resource_status=lambda p, sid, states, **k:
            events.append(('cleanup_started' if 'started' in states else 'cleanup_completed', (sid, states))),
        _wait_for_cleanup_resume_summary_or_completion=lambda *a:
            pytest.fail('a missing five-second summary must not start a full stream wait'),
        _expect_raw_input_ready=lambda *a, **kw: events.append(('resume_input_ready', kw)),
        _cleanup_resource_for_stack=lambda p, sid: cleanup if sid == old else None,
        _cleanup_resource_completed=actual_legacy._cleanup_resource_completed,
        _capture_ros_stack_states=lambda *a: {
            old: {'status': 'DELETE_COMPLETE' if old_deleted else 'CREATE_COMPLETE'},
            new: {'status': 'CREATE_COMPLETE'}},
        _ros_stack_deleted=actual_legacy._ros_stack_deleted,
        _ros_stack_retained=actual_legacy._ros_stack_retained,
    )
    monkeypatch.setattr(runner, '_legacy_repl_module', lambda: legacy)
    def spawn(**kw):
        cleanup['cleanup_status'] = 'pending'
        events.append(('spawn', kw))

    def resumed_wait(p, sid, states, **kw):
        events.append(('cleanup_started' if 'started' in states else 'cleanup_completed', (sid, states)))
        if states == {'completed'}:
            cleanup['cleanup_status'] = 'completed' if old_deleted else 'failed'

    legacy._wait_for_cleanup_resource_status = resumed_wait
    def resumed_cleanup(p, r, sid, **kw):
        events.append(('resume_observation', kw))
        r.checks['REPL explicit cleanup continuation submitted'] = True
        p.sendline_reliable('continue this session cleanup')
        resumed_wait(p, sid, {'completed'})

    monkeypatch.setattr(runner, '_repl_wait_cleanup_after_restart',
                        lambda r, p, sid, **kw: resumed_cleanup(p, r, sid, **kw))
    pty = SimpleNamespace(send=lambda *a, **kw: events.append(('send', kw)),
                          terminate=lambda **kw: events.append(('terminate', kw)),
                          spawn=spawn, transcript='old process input marker',
                          sendline_reliable=lambda text: events.append(('manual_cleanup_continue', text)))
    runner._run_repl_cleanup_recovery(runtime, pty)
    interrupted_goal = next(payload for name, payload in events if name == 'interrupt')
    assert 'VpcId=vpc-independent-fixture' in interrupted_goal
    assert '不得依赖旧 Stack 创建的 VPC' in interrupted_goal
    assert events.index(('cleanup_started', (old, {'started', 'in_progress'}))) < events.index(
        ('terminate', {'force': True}))
    assert ('spawn', {'extra_args': ['--continue']}) in events
    assert events.count(('selection', {})) == 2
    assert events.index(('_repl_wait_pipeline_completed', {})) < events.index(
        ('cleanup_started', (old, {'started', 'in_progress'})))
    assert events.index(('spawn', {'extra_args': ['--continue']})) < events.index(
        ('cleanup_completed', (old, {'completed'})))
    assert runtime.checks['cleanup snapshot does not target new Stack'] is True
    assert runtime.checks['rollback cleanup observed two distinct Stacks'] is True
    assert runtime.checks['old Stack cleanup completed after restart'] is old_deleted
    assert runtime.checks['ROS old Stack deleted before teardown'] is old_deleted
    assert runtime.checks['ROS new Stack retained before teardown'] is True
    assert runtime.checks['REPL explicit cleanup continuation submitted'] is True
    assert len([event for event in events if event[0] == 'manual_cleanup_continue']) == 1
    ready = next(event for event in events if event[0] == 'resume_observation')
    assert ready[1]['since_offset'] == len('old process input marker')


def test_cleanup_recovery_missing_independent_fixture_fails_before_first_deployment(runner, monkeypatch):
    monkeypatch.setattr(runner, '_resolve_runtime_question_facts', lambda *a: {})
    monkeypatch.setattr(runner, '_repl_submit_initial_prompt',
                        lambda *a: pytest.fail('created cloud resources without independent fixture'))
    with pytest.raises(RuntimeError, match='independent existing VPC fixture'):
        runner._run_repl_cleanup_recovery(SimpleNamespace(), SimpleNamespace())


@pytest.mark.parametrize('input_ready', [False, True])
def test_restart_cleanup_observes_real_completion_without_requiring_a_prompt(
    runner, monkeypatch, input_ready,
):
    legacy = runner._legacy_repl_module()
    resource = {'cleanup_status': 'in_progress', 'progress_status': 'DELETE_IN_PROGRESS'}
    monkeypatch.setattr(legacy, '_cleanup_resource_for_stack', lambda *a: resource)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    marker = '\x1b[>4;2m'
    sends = []
    drains = []
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=1), checks={})
    pty = SimpleNamespace(transcript='old process' + marker,
                          sendline_reliable=lambda text: sends.append(text))
    offset = len(pty.transcript)
    def drain():
        drains.append(True)
        if input_ready and len(drains) == 1:
            pty.transcript += marker
        if len(drains) == 3:
            resource.update(cleanup_status='completed', progress_status='DELETE_COMPLETE')
    pty.drain_output = drain
    runner._repl_wait_cleanup_after_restart(runtime, pty, 'actual-created-stack', since_offset=offset)
    assert resource['progress_status'] == 'DELETE_COMPLETE'
    assert len(sends) == (1 if input_ready else 0)
    assert all('不要创建新资源' in text for text in sends)


def test_restart_cleanup_does_not_pass_a_failed_deletion(runner, monkeypatch):
    legacy = runner._legacy_repl_module()
    monkeypatch.setattr(legacy, '_cleanup_resource_for_stack', lambda *a: {
        'cleanup_status': 'failed', 'progress_status': 'DELETE_FAILED'})
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=1), checks={})
    pty = SimpleNamespace(transcript='', drain_output=lambda: None,
                          sendline_reliable=lambda _: pytest.fail('failed deletion must not be rescued'))
    with pytest.raises(RuntimeError, match='cleanup failed'):
        runner._repl_wait_cleanup_after_restart(runtime, pty, 'actual-created-stack', since_offset=0)


def test_redaction_fixture_keeps_database_noecho_real_pricing_and_no_deployment(runner):
    spec = next(spec for spec in runner.SCENARIOS if spec.profile == 'redaction')
    runtime = SimpleNamespace(spec=spec, cidr='10.22.0.0/24')
    prompt = runner._initial_prompt(runtime)
    assert '收费数据库' in prompt and 'NoEcho' in prompt
    assert '真实询价数字' in prompt and '只到部署确认，不创建资源' in prompt
    assert '三类字符都要包含' in prompt and '以实际约束为准' in prompt
    assert '脱敏仅用于公开展示' in prompt


def test_reselect_fixture_has_compatible_network_and_complete_replacement_goal(runner):
    spec = next(spec for spec in runner.SCENARIOS if spec.profile == 'reselect_new_intent')
    runtime = SimpleNamespace(spec=spec, cidr='10.22.0.0/24')
    prompt = runner._initial_prompt(runtime)
    assert 'VPC 网段必须覆盖' in prompt and '10.22.0.0/24' in prompt
    assert '不引入 ECS' in prompt and '本轮不部署' in prompt
    plan = runner._a2a_plan(runtime)
    replacement = plan.confirmation_answers[1]
    assert '我改需求了：只创建一个安全组' in replacement
    assert '安全组为空' in replacement and '不开放公网来源' in replacement
    assert '本轮只进行 Preview 和询价' in replacement


@pytest.mark.parametrize('profile', ['natural_adjust', 'rollback_cleanup', 'rollback_cleanup_recovery',
                                     'image_interrupt', 'happy_single', 'fault_checkpoints'])
def test_network_adjustment_and_cleanup_fixtures_define_one_unambiguous_new_subnet(runner, profile):
    spec = next(spec for spec in runner.SCENARIOS if spec.profile == profile)
    runtime = SimpleNamespace(spec=spec, cidr='10.22.0.0/24')
    prompt = runner._initial_prompt(runtime)
    assert '至少给出两个详细架构方案' in prompt
    assert '只新建一个 VPC 和一个 VSwitch' in prompt
    assert '不复用已有 VPC' in prompt
    assert '不引入 NAT、公网 IP、ECS 或数据库' in prompt
    assert '每个候选都必须包含这两个新建资源' in prompt
    assert '不同的真实可用区' in prompt
    assert 'VPC 网段必须覆盖' in prompt and runtime.cidr in prompt
    assert 'VSwitch 初始网段使用上述预留值' in prompt
    assert 'StackName' not in prompt and '资源栈名称' not in prompt
    if profile.startswith('rollback'):
        assert '稍后我会改变部署目标' in prompt



def test_current_literal_parameters_override_old_fixture_defaults(runner):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='natural_adjust'), cidr='10.0.0.0/24', stack_name='',
        question_facts={'cidr': '10.0.0.0/24', 'vpc_id': 'vpc-old', 'zone_id': 'cn-hangzhou-i'},
        current_goal='把 VSwitch 网段调整为 10.0.0.128/25，复用 vpc-new 和 cn-hangzhou-j。')
    facts = runner._question_facts(runtime)
    assert facts['cidr'] == '10.0.0.128/25' and facts['cidr_prefix'] == '25'
    assert facts['vpc_id'] == 'vpc-new' and facts['zone_id'] == 'cn-hangzhou-j'
    assert runtime.question_facts['cidr'] == '10.0.0.0/24'  # Fixture remains a fixture, not new user input.


def test_natural_adjustment_fixture_does_not_require_initial_subnet_as_final_constraint(runner):
    initial = '10.22.0.0/24'
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='natural_adjust'), cidr=initial)
    prompt = runner._initial_prompt(runtime)
    assert f'预留网段 {initial} 作为首次 Preview 的初始值' in prompt
    assert '后续允许只调整 VSwitch 网段' in prompt and '最新请求为准' in prompt
    assert f'如需 VSwitch 使用 runner 预留网段 {initial}' not in prompt
    # The change is scoped to a scenario whose purpose is a parameter edit.
    runtime.spec.profile = 'happy_single'
    ordinary = runner._initial_prompt(runtime)
    assert f'如需 VSwitch 使用 runner 预留网段 {initial}' in ordinary
    assert '最新请求为准' not in ordinary


def test_tool_names_in_text_outputs_and_schemas_do_not_count_as_native_calls(runner):
    values = [{'name': 'ros_preview_template'}, {'name': 'ros_estimate_template_cost'},
              {'role': 'user', 'content': [{'type': 'tool_result', 'content': {'role': 'assistant', 'content': [
                  {'type': 'tool_use', 'id': 'fake', 'name': 'ros_preview_template'}]}}]}]
    assert runner._native_tool_use_names(values) == []
    real = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'real', 'name': 'ros_preview_template'}]}
    assert runner._native_tool_use_names([real, real]) == ['ros_preview_template']


def _preview_rows(cidr):
    return [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'preview', 'name': 'ros_preview_template',
                                             'input': {'template_url': 'network.yaml',
                                                       'parameters': {'Subnet': cidr}}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'preview', 'is_error': False,
               'content': json.dumps({'Stack': {'Resources': [{'ResourceType': 'ALIYUN::ECS::VSwitch',
                                                              'Properties': {'CidrBlock': cidr}}]}})}]}]


@pytest.mark.parametrize(('preview_cidr', 'matches'), [('10.0.0.0/24', False), ('10.0.0.128/25', True)])
def test_adjustment_preview_checkpoint_distinguishes_step2_from_deployment(runner, monkeypatch, tmp_path,
                                                                        preview_cidr, matches):
    runtime = SimpleNamespace(paths=SimpleNamespace(workspace_dir=tmp_path, config_dir=tmp_path),
                              diagnostics={}, checks={'existing acceptance': True})
    monkeypatch.setattr(runner, '_read_repl_transcript_values', lambda _: _preview_rows(preview_cidr))
    runner._record_repl_adjustment_preview(runtime, '10.0.0.128/25')
    assert runtime.diagnostics['repl_adjustment_preview_cidr_inspected'] is True
    assert runtime.diagnostics['repl_adjustment_preview_matches_requested_cidr'] is matches
    assert runtime.checks == {'existing acceptance': True}
    assert preview_cidr not in json.dumps(runtime.diagnostics)


def test_initial_cidr_is_from_correlated_native_preview_not_quoted_schema(runner):
    rows = _preview_rows('10.0.0.0/24')
    assert runner._initial_preview_vswitch_cidrs(rows) == ['10.0.0.0/24']
    rows[-1]['content'][0]['tool_use_id'] = 'another-tool'
    assert runner._initial_preview_vswitch_cidrs(rows) == []
    rows[-1]['content'][0]['tool_use_id'] = 'preview'
    rows[-1]['content'][0]['is_error'] = True
    assert runner._initial_preview_vswitch_cidrs(rows) == []


def test_initial_cidr_fallback_reads_exact_preview_template_and_parameters(runner, tmp_path):
    rows = _preview_rows('10.0.0.128/25')
    rows[-1]['content'][0]['content'] = json.dumps({'Stack': {'Resources': []}})
    template = {'Resources': {'Subnet': {'Type': 'ALIYUN::VPC::VSwitch',
                                         'Properties': {'CidrBlock': {'Ref': 'Subnet'}}}}}
    (tmp_path / 'network.yaml').write_text(yaml.safe_dump(template), encoding="utf-8")
    assert runner._initial_preview_vswitch_cidrs(rows, allowed_roots=(tmp_path,)) == ['10.0.0.128/25']
    rows[0]['content'][0]['input']['template_url'] = '../outside.yaml'
    assert runner._initial_preview_vswitch_cidrs(rows, allowed_roots=(tmp_path,)) == []


def test_requested_cidr_cannot_be_a_noop_in_the_real_initial_preview(runner):
    runtime = SimpleNamespace(cidr='10.0.0.0/24')
    target = runner._repl_natural_adjusted_cidr(runtime, ['10.0.0.0/25', '10.0.0.128/25'])
    assert target == '10.0.0.64/26'


@pytest.mark.parametrize('actual_cidr,verified', [('10.0.0.128/25', True), ('10.0.0.0/24', False)])
def test_native_cidr_probe_requires_actual_owned_vswitch_value(runner, tmp_path, monkeypatch, capsys,
                                                             actual_cidr, verified):
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
    from scripts.repl.e2e import run_pipeline_scenarios as repl
    resource = {'stackId': 'accepted-stack', 'stackName': 'application-name', 'regionId': 'cn-hangzhou',
                'ownershipSource': 'accepted_create_ledger'}
    manifest = tmp_path / 'probe.json'
    manifest.write_text(json.dumps({'expected_cidr': '10.0.0.128/25', 'resources': [resource]}), encoding="utf-8")
    queried = []
    class Client:
        def get_stack(self, request):
            assert request.stack_id == 'accepted-stack'
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                'StackName': 'application-name', 'Status': 'CREATE_COMPLETE'}))
        def list_stack_resources(self, request):
            assert request.stack_id == 'accepted-stack'
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {'Resources': [
                {'ResourceType': 'ALIYUN::ECS::VSwitch', 'PhysicalResourceId': 'vsw-owned',
                 'StackId': 'accepted-stack'}]}))
    monkeypatch.setattr(CloudCredentials, 'get_provider', lambda *_: SimpleNamespace(region_id='cn-hangzhou'))
    monkeypatch.setattr(RosClientFactory, 'create', lambda *_: Client())
    def api(product, action, params):
        queried.append((product, action, params))
        return {'VSwitchId': 'vsw-owned', 'CidrBlock': actual_cidr}
    monkeypatch.setattr(repl, '_call_aliyun_api', api)
    monkeypatch.setattr(sys, 'argv', ['probe', str(manifest)])
    exec(runner._CLOUD_ADJUSTMENT_PROBE_CODE, {})
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result == {'owned_stack_count': 1, 'vswitch_count': 1, 'matching_vswitch_count': int(verified),
                      'cidr_verified': verified,
                      'actual_cidr_hashes': [hashlib.sha256(actual_cidr.encode()).hexdigest()]}
    assert queried == [('vpc', 'DescribeVSwitchAttributes', {'RegionId': 'cn-hangzhou', 'VSwitchId': 'vsw-owned'})]
    assert 'accepted-stack' not in output and 'vsw-owned' not in output



def test_native_cidr_probe_never_queries_resources_without_matching_case_ownership(
    runner, tmp_path, monkeypatch, capsys,
):
    from iac_code.services.cloud_credentials import CloudCredentials
    from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
    from scripts.repl.e2e import run_pipeline_scenarios as repl
    manifest = tmp_path / 'probe.json'
    resource = {'stackId': 'accepted-stack', 'stackName': 'application-name', 'regionId': 'cn-hangzhou',
                'ownershipSource': 'accepted_create_ledger'}
    manifest.write_text(json.dumps({'expected_cidr': '10.0.0.128/25', 'resources': [resource]}), encoding="utf-8")
    class Client:
        def get_stack(self, request):
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {
                'StackName': 'someone-elses-name', 'Status': 'CREATE_COMPLETE'}))
        def list_stack_resources(self, request):
            pytest.fail('ownership mismatch must prevent a resource query')
    monkeypatch.setattr(CloudCredentials, 'get_provider', lambda *_: SimpleNamespace(region_id='cn-hangzhou'))
    monkeypatch.setattr(RosClientFactory, 'create', lambda *_: Client())
    monkeypatch.setattr(repl, '_call_aliyun_api', lambda *_: pytest.fail('no VPC query on an unowned Stack'))
    monkeypatch.setattr(sys, 'argv', ['probe', str(manifest)])
    with pytest.raises(SystemExit) as error:
        exec(runner._CLOUD_ADJUSTMENT_PROBE_CODE, {})
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['probe_error_type'] == 'RuntimeError'
    assert 'someone-elses-name' not in json.dumps(result) and 'accepted-stack' not in json.dumps(result)


def test_natural_adjustment_parameter_questions_use_submitted_new_cidr(runner, monkeypatch, tmp_path):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='natural_adjust'), cidr='10.0.0.0/24', stack_name='',
        question_facts={'cidr': '10.0.0.0/24'}, current_goal='只创建一个 VSwitch，网段 10.0.0.0/24。',
        paths=SimpleNamespace(workspace_dir=tmp_path, config_dir=tmp_path), checks={}, diagnostics={})
    monkeypatch.setattr(runner, '_repl_submit_initial_prompt', lambda *_: None)
    monkeypatch.setattr(runner, '_repl_wait_selection', lambda *_, **__: None)
    monkeypatch.setattr(runner, '_repl_select_current', lambda *_, **__: None)
    monkeypatch.setattr(runner, '_read_repl_transcript_values', lambda *_: _preview_rows('10.0.0.0/24'))
    monkeypatch.setattr(runner, '_read_repl_display_events', lambda *_: [])
    submitted = []
    observed_facts = []
    monkeypatch.setattr(runner, '_repl_choose_direct_input', lambda _, __, text: submitted.append(text))
    def wait_for_confirmation(*_):
        if submitted:
            observed_facts.append(runner._question_facts(runtime)['cidr'])
    monkeypatch.setattr(runner, '_repl_wait_confirmation_after_optional_parameter_asks', wait_for_confirmation)
    runner._repl_basic_flow(runtime, object())
    assert '10.0.0.128/25' in submitted[0]
    assert observed_facts == ['10.0.0.128/25']
    assert runtime.requested_adjusted_cidr == '10.0.0.128/25'
    assert runtime.question_facts['cidr'] == '10.0.0.0/24'


def test_initial_preview_reads_owned_externalized_result_without_exporting_body(runner, tmp_path):
    rows = _preview_rows('10.0.0.0/24')
    block = rows[-1]['content'][0]
    path = tmp_path / 'preview-result.txt'
    path.write_text(block['content'], encoding="utf-8")
    block['content'] = '{truncated preview output'
    block['metadata'] = {'_iac_code_externalized_result_path': str(path)}
    diagnostics = {}
    assert runner._initial_preview_vswitch_cidrs(rows, allowed_roots=(tmp_path,), diagnostics=diagnostics) == [
        '10.0.0.0/24']
    assert diagnostics == {'repl_initial_cidr_probe_stage': 'native_cidr', 'repl_initial_preview_call_count': 1}
    assert str(path) not in json.dumps(diagnostics)


def test_initial_preview_supports_native_ros_shorthand_in_exact_template(runner, tmp_path):
    rows = _preview_rows('10.0.0.128/25')
    rows[-1]['content'][0]['content'] = json.dumps({'Stack': {'Resources': []}})
    (tmp_path / 'network.yaml').write_text(
        "ROSTemplateFormatVersion: 2015-09-01\nResources:\n  Subnet:\n"
        "    Type: ALIYUN::ECS::VSwitch\n    Properties:\n      CidrBlock: !Ref Subnet\n"
    , encoding="utf-8")
    diagnostics = {}
    assert runner._initial_preview_vswitch_cidrs(rows, allowed_roots=(tmp_path,), diagnostics=diagnostics) == [
        '10.0.0.128/25']
    assert diagnostics['repl_initial_cidr_probe_stage'] == 'template_cidr'


def test_nonlocal_case_telemetry_keeps_remote_route_and_uses_isolated_e2e_id(runner, tmp_path):
    path = tmp_path / 'settings.yml'
    path.write_text('userID: real-user\nmodel: fake-model\n', encoding="utf-8")
    env = {'IAC_CODE_TELEMETRY_LOCAL_ONLY': '1', 'IAC_CODE_ENABLE_LOCAL_TELEMETRY': '1',
           'IAC_CODE_TELEMETRY_ENDPOINT': 'https://telemetry.example.invalid'}
    runner._prepare_case_telemetry_identity(tmp_path, env, local_capture=False)
    settings = yaml.safe_load(path.read_text( encoding="utf-8"))
    assert settings['model'] == 'fake-model'
    assert settings['userID'].startswith('iac_user_e2e_')
    assert env['IAC_CODE_TELEMETRY_E2E_USER_ID'] == settings['userID']
    assert env['IAC_CODE_TELEMETRY_ENDPOINT'] == 'https://telemetry.example.invalid'
    assert 'IAC_CODE_TELEMETRY_LOCAL_ONLY' not in env and 'IAC_CODE_ENABLE_LOCAL_TELEMETRY' not in env
    runner._prepare_case_telemetry_identity(tmp_path, env, local_capture=False)
    assert yaml.safe_load(path.read_text( encoding="utf-8"))['userID'] == settings['userID']


def test_local_telemetry_audit_preserves_e2e_identity_and_loopback_guard(runner, tmp_path):
    path = tmp_path / 'settings.yml'
    user_id = 'iac_user_e2e_' + 'a' * 32
    path.write_text('userID: ' + user_id + '\n', encoding="utf-8")
    env = {}
    runner._prepare_case_telemetry_identity(tmp_path, env, local_capture=True)
    assert env['IAC_CODE_TELEMETRY_E2E_USER_ID'] == user_id
    assert env['IAC_CODE_TELEMETRY_LOCAL_ONLY'] == '1'
    assert yaml.safe_load(path.read_text( encoding="utf-8"))['userID'] == user_id


@pytest.mark.parametrize('scenario,expects_local', [('a2a-safe-quote-cancel', False), ('a2a-happy-multi-plan', True)])
def test_scenario_uses_loopback_only_for_explicit_telemetry_audit(
    runner, tmp_path, monkeypatch, scenario, expects_local,
):
    import scripts.observability.local_observe.e2e_audit as audit
    source = tmp_path / 'source'
    source.mkdir()
    for name in runner.CREDENTIAL_FILES:
        (source / name).write_text('fake: value\n', encoding="utf-8")
    (source / 'settings.yml').write_text('userID: real-user\n', encoding="utf-8")
    args = runner.parse_args(['--scenario', scenario, '--run-root', str(tmp_path / 'runs'),
        '--credential-source-dir', str(source), '--inherit-settings'])
    captures = []
    dispatched = []
    class Capture:
        env = {'IAC_CODE_ENABLE_LOCAL_TELEMETRY': '1', 'IAC_CODE_TELEMETRY_ENDPOINT': 'http://127.0.0.1:1'}
        def __init__(self, _):
            captures.append(self)
        def start(self):
            return self
        def stop(self):
            return [{'kind': 'span'}]
    monkeypatch.setenv('IAC_CODE_TELEMETRY_LOCAL_ONLY', '1')
    monkeypatch.setenv('IAC_CODE_TELEMETRY_ENDPOINT', 'http://localhost:9999')
    monkeypatch.setattr(audit, 'ObserveCapture', Capture)
    monkeypatch.setattr(audit, 'audit_provider_attempts', lambda *_, **__: {'passed': True})
    def dispatch(runtime):
        settings = yaml.safe_load((runtime.paths.config_dir / 'settings.yml').read_text( encoding="utf-8"))
        assert settings['userID'] == runtime.env['IAC_CODE_TELEMETRY_E2E_USER_ID']
        assert settings['userID'].startswith('iac_user_e2e_')
        assert ('IAC_CODE_TELEMETRY_ENDPOINT' in runtime.env) is expects_local
        runtime.checks['original business acceptance'] = True
        dispatched.append(True)
    monkeypatch.setattr(runner, '_dispatch_surface', dispatch)
    monkeypatch.setattr(runner, 'run_public_contract_audit', lambda *_: None)
    monkeypatch.setattr(runner, 'collect_templates', lambda *_: None)
    monkeypatch.setattr(runner, 'apply_profile_acceptance', lambda *_: None)
    monkeypatch.setattr(runner, 'cleanup_cloud_resources', lambda *_: 'completed')
    result = runner.run_one_scenario(runner.SCENARIO_BY_NAME[scenario], args, runner.RunnerServices(), {}, tmp_path)
    assert result.status == 'passed' and result.checks['original business acceptance']
    assert bool(captures) is expects_local
    assert ('real telemetry captured' in result.checks) is expects_local
    if expects_local:
        assert result.checks['provider telemetry has unique terminal records'] is True
    assert yaml.safe_load((source / 'settings.yml').read_text( encoding="utf-8"))['userID'] == 'real-user'


def test_partial_adjustment_keeps_prior_region_and_confirmed_zone_not_old_subnet(runner):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile='natural_adjust'), cidr='10.0.0.0/24', stack_name='',
        question_facts={'cidr': '10.0.0.0/24'}, current_goal='请在阿里云杭州部署一个测试应用的网络。')
    goal = '仅把 VSwitch 网段调整为 10.0.0.128/25，其余参数保持刚才方案，重新 Preview 和询价。'
    runner._remember_partial_adjustment_context(runtime, goal, {'ZoneId': 'cn-hangzhou-i', 'VpcId': 'vpc-confirmed'})
    runtime.current_goal = goal
    facts = runner._question_facts(runtime)
    assert facts['cloud_vendor'] == '阿里云' and facts['region'] == 'cn-hangzhou'
    assert facts['zone_id'] == 'cn-hangzhou-i' and facts['vpc_id'] == 'vpc-confirmed'
    assert facts['cidr'] == '10.0.0.128/25'
    assert '其余参数保持刚才方案' in facts['constraints']
    assert runtime.question_facts['cidr'] == '10.0.0.0/24'
    runtime.current_goal = '我改需求了：在香港只创建安全组。'
    replacement = runner._question_facts(runtime)
    assert 'zone_id' not in replacement and 'vpc_id' not in replacement


def test_confirmation_wait_observes_live_replanning_question_in_step1(runner, monkeypatch, tmp_path):
    path = tmp_path / 'projects' / 'project' / 'session' / 'pipeline' / 'meta.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({'current_step': runner.NEW_STEPS[0], 'execution': {
        'pending_input_kind': 'ask_user_question', 'pending_ask_user_question_input': {
            'toolUseId': 'new-planning-question', 'question': '现有目标是否保持？', 'allowFreeText': True}}}),
        encoding="utf-8")
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path, run_dir=tmp_path))
    monkeypatch.setattr(runner, '_read_repl_display_events', lambda *_: [])
    pending = runner._pending_repl_input_before_confirmation(runtime, set())
    assert pending and pending[0]['step_id'] == runner.NEW_STEPS[0]
    assert pending[0]['payload']['tool_use_id'] == 'new-planning-question'
    assert runner._pending_repl_input_before_confirmation(runtime, {'new-planning-question'}) is None


@pytest.mark.skipif(os.name == "nt", reason="PTY input replay requires POSIX")
def test_active_repl_question_answer_reaches_real_console_choice_without_paste_escapes(runner, tmp_path, monkeypatch):
    """Live questions use Console.input; restored questions use PromptInput."""
    import pexpect

    program = '''
import asyncio, json
from unittest.mock import MagicMock
from rich.console import Console
from iac_code.ui.renderer import Renderer
from iac_code.types.stream_events import AskUserQuestionEvent
async def main():
    event = AskUserQuestionEvent(tool_use_id="fake-question", question="Choose", options=[
        {"id":"network", "label":"Network"}, {"id":"cancel", "label":"Cancel"}],
        allow_free_text=False, response_future=asyncio.get_running_loop().create_future())
    answer = await Renderer(Console(), MagicMock()).prompt_user_question(event)
    print("NATIVE_ANSWER=" + json.dumps(answer, sort_keys=True), flush=True)
asyncio.run(main())
'''
    env = {k: v for k, v in os.environ.items() if k in {'PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT'}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), IAC_CODE_CONFIG_DIR=str(tmp_path),
               IAC_CODE_USER_ID='iac_user_e2e_offline', TERM='dumb')
    child = pexpect.spawn(sys.executable, ['-c', program], env=env, encoding='utf-8', timeout=2)

    class Pty:
        events = []
        def send(self, text, *, label):
            child.send(text)
        def drain_output(self):
            pass

    def acknowledge(*args, **kwargs):
        child.expect('NATIVE_ANSWER=')
        child.expect(pexpect.EOF)
        assert json.loads(child.before.strip())['selected_id'] == 'network'

    try:
        child.expect('  > ', timeout=10)  # Interpreter/import startup under parallel unit-test load.
        monkeypatch.setattr(runner, '_repl_wait_question_acknowledgement', acknowledge)
        runner._repl_submit_question_answer(Pty(), SimpleNamespace(), '1', ({}, tmp_path), label='active-choice')
    finally:
        child.close(force=True)


def test_backup_window_answers_actual_question_with_native_step_not_fixed_response(runner, tmp_path, monkeypatch):
    runtime = SimpleNamespace(args=SimpleNamespace(stream_timeout=10, timeout=10),
                              spec=SimpleNamespace(profile='input_during_backup', name='test'), cidr='192.168.1.0/24')
    question = {'kind': 'ask_user_question', 'question': '产品用途与已有 VPC?', 'allowFreeText': True,
                'step': {'id': runner.NEW_STEPS[0]}}
    current = SimpleNamespace(events=[], summary=SimpleNamespace())
    class BoundaryVerifiedError(Exception):
        pass
    class Harness:
        def start_stream(self, **kwargs):
            if kwargs['name'] == 'backup-window-01-initial':
                return current
            pytest.fail('native question driver was bypassed for a fixed response')
        def fetch_state(self, name):
            return {'snapshot': {'pendingInput': question}}
    monkeypatch.setattr(runner, '_wait_a2a_backup_window_started', lambda *_: {'startedMonotonic': 1})
    monkeypatch.setattr(runner, '_arm_a2a_backup_delay', lambda *_: tmp_path / 'next')
    def answer(_runtime, pending, **_kwargs):
        assert pending['question'] == question['question']
        assert pending['_step_id'] == runner.NEW_STEPS[0]
        raise BoundaryVerifiedError
    monkeypatch.setattr(runner, '_answer_runtime_question', answer)
    with pytest.raises(BoundaryVerifiedError):
        runner._run_a2a_input_during_backup(runtime, Harness(), object(),
            runner.A2AConversationPlan(ask_answers=['fixed answer']), tmp_path / 'first')


def test_rollback_step2_fault_wait_answers_new_candidate_batch_before_crashing(runner, tmp_path, monkeypatch):
    """A native Step 1 reselection handoff must not be mistaken for a missing Step 2."""
    runtime = SimpleNamespace(args=SimpleNamespace(timeout=10, stream_timeout=10),
        paths=SimpleNamespace(run_dir=tmp_path), checks={}, current_goal='old goal', event=lambda *a, **k: None)
    plan = SimpleNamespace(confirmation_answers=[])
    monkeypatch.setattr(runner, '_advance_a2a_to_pending', lambda *a, **k: None)
    monkeypatch.setattr(runner, '_continue_a2a_to_pending', lambda *a, **k: a[4])
    monkeypatch.setattr(runner, '_continue_a2a_from_summary', lambda *a, **k: None)
    responses = []
    def answer(*args):
        responses.append(args[1])
        return '{"action":"select","candidate_index":0}', ''
    monkeypatch.setattr(runner, '_a2a_response_for_pending', answer)
    observed = []
    class Stream:
        exception = None
        done = True
        def __init__(self, name, step, retry_selection=False):
            self.name = name
            self.events = [{"eventType": "input_required", "step": {"id": runner.NEW_STEPS[0]},
                "data": {"kind": "candidate_selection"}}] if retry_selection else [
                {"eventType": "step_started", "step": {"id": step}}]
            (tmp_path / f"{name}.events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in self.events), encoding="utf-8")
            self.summary = SimpleNamespace(name=name, last_status_state='TASK_STATE_INPUT_REQUIRED',
                last_input_required_step_id=runner.NEW_STEPS[0])
        def wait_for(self, predicate, *, description, timeout):
            for event in self.events:
                if predicate(event, self.summary):
                    observed.append(description)
                    return
            raise RuntimeError(f'{self.name} ended before {description}')
        def join(self, timeout=None):
            return self.summary
    starts = []
    class Harness:
        pipeline_task_id = 'same-task'
        def start_stream(self, **kwargs):
            starts.append(kwargs['name'])
            step = runner.NEW_STEPS[0] if len(starts) == 1 else runner.NEW_STEPS[1]
            return Stream(kwargs['name'], step, len(starts) == 2)
        def kill9_and_restart(self):
            assert observed[-1] == 'rollback Step 2 started'
            observed.append('crash')
        def stream(self, **kwargs):
            assert observed[-1] == 'crash'
            return SimpleNamespace(task_id='same-task')
    a2a = runner._legacy_a2a_module()
    runner._run_a2a_rollback_recovery(runtime, Harness(), a2a, plan, runner.NEW_STEPS[1])
    assert responses == ['candidate_selection', 'candidate_selection']
    assert observed == ['rollback Step 1 started', 'rollback Step 2 started', 'crash']
    assert runtime.checks[f'rollback {runner.NEW_STEPS[1]} restored same task'] is True


@pytest.mark.parametrize(('state', 'step', 'kind', 'exception'), [
    ('TASK_STATE_COMPLETED', 'solution_planning_and_selection', 'candidate_selection', None),
    ('TASK_STATE_FAILED', 'solution_planning_and_selection', 'candidate_selection', None),
    ('TASK_STATE_INPUT_REQUIRED', 'materialize_selected_candidate', 'ask_user_question', None),
    ('TASK_STATE_INPUT_REQUIRED', 'solution_planning_and_selection', 'deployment_confirmation', None),
    ('TASK_STATE_INPUT_REQUIRED', 'solution_planning_and_selection', 'candidate_selection', OSError('transport')),
])
def test_rollback_fault_wait_never_rescues_errors_or_skips_checkpoint(
    runner, tmp_path, monkeypatch, state, step, kind, exception,
):
    runtime = SimpleNamespace(args=SimpleNamespace(timeout=10), paths=SimpleNamespace(run_dir=tmp_path))
    monkeypatch.setattr(runner, '_pending_kind', lambda *a: kind)
    class Stream:
        name = 'selection'
        done = True
        def wait_for(self, *args, **kwargs):
            raise RuntimeError('selection ended before rollback Step 2 started')
        def join(self, timeout=None):
            return SimpleNamespace(name=self.name, last_status_state=state, last_input_required_step_id=step)
    stream = Stream()
    stream.exception = exception
    stream.summary = SimpleNamespace(name=stream.name, last_status_state=state, last_input_required_step_id=step)
    class Harness:
        def start_stream(self, **kwargs):
            pytest.fail('unexpected new user input or rescue')
    a2a = SimpleNamespace(_step_started=lambda step: lambda *a: True)
    with pytest.raises(RuntimeError, match='ended before rollback Step 2 started'):
        runner._wait_a2a_step_with_pending_inputs(runtime, Harness(), a2a, object(), stream,
            target_step=runner.NEW_STEPS[1], description='rollback Step 2 started', name_prefix='rollback')


def test_rollback_fault_wait_uses_original_total_deadline(runner, monkeypatch):
    runtime = SimpleNamespace(args=SimpleNamespace(timeout=10))
    times = iter([100, 110])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: next(times))
    with pytest.raises(TimeoutError, match='rollback Step 2 started'):
        runner._wait_a2a_step_with_pending_inputs(runtime, object(), object(), object(), object(),
            target_step=runner.NEW_STEPS[1], description='rollback Step 2 started', name_prefix='rollback')


def test_rollback_input_handoff_does_not_require_thread_bookkeeping_to_have_finished(
    runner, tmp_path, monkeypatch,
):
    """A closed SSE stream publishes done before Thread removes itself from the registry."""
    import io

    a2a = runner._legacy_a2a_module()
    entered_transport = threading.Event()
    release_transport = threading.Event()
    envelope = {'eventType': 'input_required', 'step': {'id': runner.NEW_STEPS[0]},
        'data': {'kind': 'candidate_selection'},
        'result': {'id': 'fake-task', 'status': {'state': 'TASK_STATE_INPUT_REQUIRED'}}}
    response = io.BytesIO(('data: ' + json.dumps(envelope) + '\n\n').encode())
    def transport(*args, **kwargs):
        entered_transport.set()
        assert release_transport.wait(5)
        return response
    monkeypatch.setattr(a2a, 'urlopen', transport)
    stream = a2a.BackgroundStream(name='selection', prompt='fake selection',
        server_url='http://example.invalid', cwd=str(tmp_path),
        run_dir=tmp_path, timeout=5, context_id='fake-context', task_id='fake-task')
    runtime = SimpleNamespace(args=SimpleNamespace(timeout=10), paths=SimpleNamespace(run_dir=tmp_path),
        spec=SimpleNamespace(profile='rollback_step2'))
    plan = runner.A2AConversationPlan()
    class NextStream:
        def wait_for(self, predicate, **kwargs):
            assert predicate({'eventType': 'step_started', 'step': {'id': runner.NEW_STEPS[1]}}, None)
    next_stream = NextStream()
    replies = []
    harness = SimpleNamespace(start_stream=lambda **kwargs: replies.append(kwargs['prompt']) or next_stream)
    stream.start()
    assert entered_transport.wait(5)
    try:
        # Force the real scheduling window: the request is closed and its done
        # notification is visible, but the worker has not finished Thread._delete.
        with threading._active_limbo_lock:
            release_transport.set()
            result = runner._wait_a2a_step_with_pending_inputs(runtime, harness, a2a, plan, stream,
                target_step=runner.NEW_STEPS[1], description='rollback Step 2 started', name_prefix='rollback')
            assert result is next_stream
            assert stream.done and stream._thread.is_alive()
            assert replies == [runner._candidate_payload(0)]
    finally:
        release_transport.set()
        stream.join(timeout=5)


def test_multimodal_closed_choice_cannot_be_counted_as_image_answer(runner, tmp_path, monkeypatch):
    event = {"type": "user_input_required", "step_id": runner.NEW_STEPS[1],
             "payload": {"kind": "ask_user_question", "tool_use_id": "vpc-choice",
                         "allow_free_text": False, "question": "Which VPC?",
                         "options": [{"id": str(i), "label": "VPC"} for i in range(48)]}}
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=1), checks={})
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda *_: [])
    monkeypatch.setattr(runner, "_wait_repl_display_event", lambda *a, **kw: (event, tmp_path / "meta.yaml"))
    monkeypatch.setattr(runner, "_repl_wait_ask", lambda *a, **kw: pytest.fail("unsupported image input"))
    with pytest.raises(RuntimeError, match="does not allow free text"):
        runner._repl_wait_multimodal_confirmation(runtime, SimpleNamespace(),
            primary_image_key="ask-first-answer", phase="initial")
    assert runtime.checks["initial image question 1 acknowledged"] is False


@pytest.mark.skipif(os.name == "nt", reason="PTY image input replay requires POSIX")
@pytest.mark.parametrize("generated", [True, False])
def test_active_image_question_receives_exact_fixture_path_without_paste_controls(runner, tmp_path, generated):
    import pexpect

    program = """
import asyncio, json
from unittest.mock import MagicMock
from rich.console import Console
from iac_code.ui.renderer import Renderer
from iac_code.types.stream_events import AskUserQuestionEvent
async def main():
    event = AskUserQuestionEvent(tool_use_id="fake-image-question", question="Supply image", options=[
        {"id":"default", "label":"Default"}], allow_free_text=True)
    answer = await Renderer(Console(), MagicMock()).prompt_user_question(event)
    print("NATIVE_IMAGE_ANSWER=" + json.dumps(answer), flush=True)
asyncio.run(main())
"""
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), IAC_CODE_CONFIG_DIR=str(tmp_path),
               IAC_CODE_USER_ID="iac_user_e2e_offline", TERM="dumb")
    child = pexpect.spawn(sys.executable, ["-c", program], env=env, encoding="utf-8", timeout=2)

    class Pty:
        def __init__(self):
            self.events = []
        def send(self, text, *, label):
            child.send(text)
        def drain_output(self):
            pass
        def paste_image_fixture(self, key, *, line_input=False):
            self.env = {}
            self.transcript = ""
            self._require_child = lambda: child
            self._capture_child_output_force = lambda _text: None
            return runner._legacy_repl_module().ReplPty.paste_image_fixture(self, key, line_input=line_input)

    pty = Pty()
    runtime = SimpleNamespace(paths=SimpleNamespace(run_dir=tmp_path))
    try:
        child.expect("  > ", timeout=10)
        # This is the actual active-question caller, not a synthetic key sequence.
        if generated:
            runner._repl_submit_generated_image(runtime, pty, "ask-first-answer", "Choose the first VPC",
                                                 label="initial-image-ask-enter-1", line_input=True)
        else:
            runner._repl_submit_image_fixture(pty, "ask-first-answer", label="initial-image-ask-enter-1",
                                              line_input=True)
        child.expect("NATIVE_IMAGE_ANSWER=")
        child.expect(pexpect.EOF)
        answer = json.loads(child.before.strip())
        path = next(e["path"] for e in pty.events if e.get("type") == "paste-image-fixture")
        assert Path(path).is_file()
        assert answer["free_text"] == path
    finally:
        child.close(force=True)


@pytest.mark.parametrize('new_confirmation', [False, True])
def test_repl_completion_reports_only_a_new_confirmation_after_accepted_final_input(
    runner, monkeypatch, tmp_path, new_confirmation,
):
    display = tmp_path / 'projects/p/s/pipeline/display.jsonl'
    display.parent.mkdir(parents=True)
    required = {'type': 'user_input_required', 'step_id': runner.NEW_STEPS[1],
                'payload': {'kind': 'deployment_confirmation'}}
    events = [{'type': 'step_started', 'step_id': runner.NEW_STEPS[1]}, required]
    boundary = len(events)
    if new_confirmation:
        events.extend([
            {'type': 'user_input_received', 'step_id': runner.NEW_STEPS[1],
             'payload': {'kind': 'deployment_confirmation', 'structured': False}}, required,
        ])
    display.write_text('\n'.join(json.dumps(row) for row in events), encoding='utf-8')
    runtime = SimpleNamespace(paths=SimpleNamespace(config_dir=tmp_path),
                              args=SimpleNamespace(stream_timeout=30), checks={}, diagnostics={},
                              repl_last_confirmation_input_boundary=boundary)
    pty = SimpleNamespace(events=[], drain_output=lambda: None)

    def wait(_runtime, **kwargs):
        assert kwargs['alternate_input']() is None
        if new_confirmation:
            raise TimeoutError('old completion wait cannot advance without another user input')
        return {'type': 'pipeline_completed'}, display

    monkeypatch.setattr(runner, '_wait_repl_display_event', wait)
    if new_confirmation:
        with pytest.raises(RuntimeError, match='another deployment confirmation after final user input'):
            runner._repl_wait_pipeline_completed(pty, runtime)
        assert runtime.checks['REPL display pipeline_completed occurrence 1 observed'] is False
        assert runtime.diagnostics['repl_completion_reconfirmation_pending'] is True
        assert pty.events == []
    else:
        runner._repl_wait_pipeline_completed(pty, runtime)
        assert runtime.checks == {} and runtime.diagnostics == {}
        assert pty.events[-1]['event_type'] == 'pipeline_completed'


def test_natural_adjustment_final_confirmation_does_not_repeat_parameter_override(runner, monkeypatch, tmp_path):
    runtime = SimpleNamespace(spec=SimpleNamespace(profile="natural_adjust", cloud_write=True),
                              cidr="10.250.0.0/24", paths=SimpleNamespace(workspace_dir=tmp_path,
                              config_dir=tmp_path), checks={}, diagnostics={})
    sent = []
    for name in ("_repl_submit_initial_prompt", "_repl_wait_selection", "_repl_select_current",
                 "_repl_wait_confirmation_after_optional_parameter_asks", "_remember_partial_adjustment_context",
                 "_record_repl_adjustment_preview"):
        monkeypatch.setattr(runner, name, lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_read_repl_transcript_values", lambda _: [])
    monkeypatch.setattr(runner, "_read_repl_display_events", lambda _: [])
    monkeypatch.setattr(runner, "_initial_preview_vswitch_cidrs", lambda *a, **kw: ["10.250.0.0/24"])
    monkeypatch.setattr(runner, "_repl_choose_direct_input", lambda r, p, text: sent.append(text))
    runner._repl_basic_flow(runtime, SimpleNamespace())
    assert len(sent) == 2 and "10.250.0.128/25" in sent[0] and "重新 Preview 和询价" in sent[0]
    assert sent[1] == "确认部署。"
    assert runtime.requested_adjusted_cidr == "10.250.0.128/25"
    assert all(runtime.checks.values())
