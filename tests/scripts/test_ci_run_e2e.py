"""Offline tests for the bounded CI E2E runner."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.ci import run_e2e


def test_dependency_probe_survives_public_report_without_cloud_identity():
    public = run_e2e._public_live_summary({'diagnostics': {
        'cleanup_dependency_owned_stacks': 2, 'cleanup_dependency_old_vpc_count': 1,
        'cleanup_dependency_new_security_group_count': 1,
        'cleanup_dependency_new_group_depends_on_old_vpc_count': 1,
        'cleanup_dependency_fixture_is_old_vpc': False,
        'cleanup_dependency_unavailable_stage': 'private-stage',
        'cleanup_dependency_vpc_id': 'private-vpc', 'cleanup_dependency_owned_stack': 'private-stack',
    }})
    assert public['diagnostics'] == {
        'cleanup_dependency_owned_stacks': 2, 'cleanup_dependency_old_vpc_count': 1,
        'cleanup_dependency_new_security_group_count': 1,
        'cleanup_dependency_new_group_depends_on_old_vpc_count': 1,
        'cleanup_dependency_fixture_is_old_vpc': False,
    }
    assert 'private' not in json.dumps(public)


def test_each_runner_invocation_shares_a_fresh_network_registry(monkeypatch, tmp_path):
    paths = []
    cutoffs = []
    cases = [run_e2e.Case(name, 'unused', (), 1, 'fast') for name in ('first', 'second')]
    monkeypatch.setattr(run_e2e, 'select_cases', lambda _: cases)

    def capture(*args, **kwargs):
        paths.append(args[-1])
        cutoffs.append(kwargs['network_fixture_before'])
        raise RuntimeError('offline subprocess boundary')

    monkeypatch.setattr(run_e2e, 'run_case', capture)
    for _ in range(2):
        assert run_e2e.main(['--suite', 'fast', '--run-dir', str(tmp_path)]) == 1
    assert paths[0] == paths[1]
    assert paths[2] == paths[3]
    assert paths[0] != paths[2]
    assert all(path.parent == tmp_path.resolve() for path in paths)
    assert cutoffs[0] == cutoffs[1] and cutoffs[2] == cutoffs[3]
    assert cutoffs[0] <= cutoffs[2]


def test_constraint_and_external_operation_diagnostics_never_export_identity():
    public = run_e2e._public_live_summary({'diagnostics': {'2c4g_constraint_categories': {
        'property:vcpu': 2, 'evidence:aliyun_api': 1, 'private-parameter': 3}},
        'control_state': {'external_operation_categories': {
            'outcome:unknown': 2, 'action:CreateStack': 2, 'identity_present': 1, 'private-id': 4}}})
    assert public['diagnostics']['2c4g_constraint_categories'] == {'property:vcpu': 2, 'evidence:aliyun_api': 1}
    assert public['control_state']['external_operation_categories'] == {
        'outcome:unknown': 2, 'action:CreateStack': 2, 'identity_present': 1}
    assert 'private' not in json.dumps(public)


def test_instance_geometry_diagnostics_keep_only_bounded_numbers_and_known_evidence_fields():
    public = run_e2e._public_live_summary({'diagnostics': {
        '2c4g_sdk_observed_sizes': [
            {'cpu': 2, 'memoryGiB': 8, 'InstanceTypeId': 'private-id'},
            {'cpu': True, 'memoryGiB': 4}, {'cpu': 2, 'memoryGiB': float('inf')},
            {'cpu': 'private-value', 'memoryGiB': 4},
        ],
        '2c4g_constraint_categories': {
            'evidence:ros_preview_template': 2, 'evidence_product:ecs': 1,
            'evidence_action:DescribeInstanceTypes': 1, 'evidence_result_leaf:MemorySize': 1,
            'evidence_action:private-command': 1, 'evidence_result_leaf:private-path': 1,
        },
    }})
    assert public['diagnostics'] == {
        '2c4g_sdk_observed_sizes': [{'cpu': 2, 'memoryGiB': 8}],
        '2c4g_constraint_categories': {
            'evidence:ros_preview_template': 2, 'evidence_product:ecs': 1,
            'evidence_action:DescribeInstanceTypes': 1, 'evidence_result_leaf:MemorySize': 1,
        },
    }
    assert 'private' not in json.dumps(public)
    assert '2c4g_sdk_observed_sizes' not in run_e2e._public_live_summary({
        'diagnostics': {'2c4g_sdk_observed_sizes': [{'cpu': 2, 'memoryGiB': 4}] * 9}
    }).get('diagnostics', {})


def test_image_handoff_checkpoints_bound_recursive_input_and_hide_private_state():
    checkpoint = {"control_state": {"task_matches": True, "taskId": "private-id", "blocker_categories": {
        "execution": 1, "agent_loop": 1, "private-activity": 3}},
        "a2a_states": ["TASK_STATE_COMPLETED", "private-state"],
        "terminal_markers": ["execution", "private-error"],
        "diagnostics": {"image_normal_handoff_checkpoints": {"after_recovery": "private-recursion"}}}
    raw = {"after_normal_followup": checkpoint, "after_recovery": checkpoint, "private-stage": checkpoint}
    public = run_e2e._public_live_summary({"diagnostics": {"image_normal_handoff_checkpoints": raw}})
    checkpoints = public["diagnostics"]["image_normal_handoff_checkpoints"]
    assert set(checkpoints) == {"after_normal_followup", "after_recovery"}
    assert set(checkpoints["after_normal_followup"]) == {"control_state", "a2a_states", "terminal_markers"}
    assert checkpoints["after_normal_followup"]["control_state"]["blocker_categories"] == {
        "execution": 1, "agent_loop": 1}
    assert checkpoints["after_recovery"]["a2a_states"] == ["TASK_STATE_COMPLETED"]
    assert "private" not in json.dumps(public)


def test_completion_intent_source_projection_keeps_only_resource_enums(tmp_path):
    from scripts.ci.live_diagnostics import _completion_failure_facts

    path = tmp_path / 'transcripts/a/session.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'content': [{'type': 'tool_use', 'name': 'complete_step', 'id': 'x',
        'input': {'conclusion': {'intent': {'resource_intents': [
            {'product': 'SLB', 'action': 'create', 'source': 'user', 'private': 'secret'},
            {'product': 'private-product', 'action': 'create', 'source': 'user'}]}}}}]}) + '\n', encoding='utf-8')
    facts = _completion_failure_facts(tmp_path)
    assert facts['completion_input_intent_sources'] == {'slb:create:user': 1}
    assert 'private' not in json.dumps(facts)
    assert 'secret' not in json.dumps(facts)


def test_public_live_summary_pending_state_uses_closed_vocabulary() -> None:
    public = run_e2e._public_live_summary({"diagnostics": {
        "repl_pending_input_kind": "ask_user_question",
        "repl_pending_step_id": "solution_planning_and_selection",
        "repl_pending_question_answered": False,
    }})
    assert public["diagnostics"] == {
        "repl_pending_input_kind": "ask_user_question",
        "repl_pending_step_id": "solution_planning_and_selection",
        "repl_pending_question_answered": False,
    }
    private = run_e2e._public_live_summary({"diagnostics": {
        "repl_pending_input_kind": {"private": "sk-fixture"},
        "repl_pending_step_id": "sk-fixture",
        "repl_pending_question_answered": "sk-fixture",
    }})
    assert private.get("diagnostics", {}) == {}


def test_default_selection_is_allowlisted_and_credential_free() -> None:
    selected = run_e2e.select_cases(run_e2e.parse_args([]))
    assert selected == list(run_e2e.FAST_CASES)
    assert len({case.name for case in run_e2e.CASES}) == len(run_e2e.CASES)
    assert all("--allow-real-cloud" not in case.args for case in run_e2e.CASES)
    assert len(run_e2e.select_cases(run_e2e.parse_args(["--suite", "full"]))) > len(selected)


def test_catalog_includes_headless_surfaces_and_excludes_browser_desktop() -> None:
    full = run_e2e.select_cases(run_e2e.parse_args(["--suite", "full", "--list"]))
    live = run_e2e.select_cases(run_e2e.parse_args(["--suite", "live", "--list"]))
    assert len(full) == 43
    assert len(live) == 109
    assert len(run_e2e.CASES) == 152
    agui = run_e2e.select_cases(run_e2e.parse_args(["--suite", "live-agui", "--list"]))
    assert {case.name for case in agui} == {"agui-selector-" + name for name in run_e2e.AGUI_SCENARIOS}
    assert len(agui) == 6
    assert all(not case.cloud_write and not case.multimodal and case.live_runner == "agui_selector" for case in agui)
    recovery = {case.name: case for case in full if "recovery-contract" in case.name}
    assert recovery["a2a-recovery-contract"].args == ("--scenario", "e3a-recovery")
    assert recovery["a2a-handoff-recovery-contract"].args == ("--scenario", "e3a-handoff-recovery")
    assert {case.name for case in live if case.live_runner.startswith("legacy_a2a")} == {
        "a2a-recovery-" + name for name in run_e2e.A2A_RECOVERY_SCENARIOS
    }
    assert {case.name for case in live if case.live_runner == "smoke"} == {
        "smoke-a2a-vpc", "smoke-acp-vpc", "smoke-headless-vpc"
    }
    fault = next(case for case in live if case.name == "a2a-recovery-fault-after-snapshot")
    assert "--deterministic" in fault.args
    assert all("--ci-teardown" in case.args for case in live if case.live_runner == "legacy_a2a")
    unsupported = {
        "ssf-" + spec.name for spec in run_e2e.SELLING_SCENARIOS
        if spec.surface.value in {"web", "desktop"}
    }
    assert len(unsupported) == 3
    assert not unsupported.intersection(case.name for case in live)
    assert all(case.script != "scripts/a2a/e2e/reconnect/run_qoder_mcp_reconnect.py" for case in live)
    selling = [case for case in live if case.name.startswith("ssf-")]
    assert len(selling) == 42
    pools = [ipaddress.IPv4Network(case.args[case.args.index("--cidr-pool") + 1]) for case in selling]
    assert len(set(pools)) == len(selling)
    assert all(pool.subnet_of(ipaddress.IPv4Network("10.250.0.0/16")) for pool in pools)


def test_missing_or_empty_stdout_summary_is_failure_data(tmp_path: Path) -> None:
    assert run_e2e._read_summary(tmp_path, "stdout.log") is None
    (tmp_path / "stdout.log").write_text("", encoding="utf-8")
    assert run_e2e._read_summary(tmp_path, "stdout.log") is None
    (tmp_path / "stdout.log").write_text('{"passed": true}\n', encoding="utf-8")
    assert run_e2e._read_summary(tmp_path, "stdout.log") == {"passed": True}


def test_live_cleanup_status_is_reported_from_teardown_checks() -> None:
    case = next(case for case in run_e2e.LIVE_CASES if case.live_runner == "repl")
    assert run_e2e._live_cleanup_status(case, {"checks": {"teardown: stacks deleted": True}}) == "completed"
    assert run_e2e._live_cleanup_status(case, {"checks": {"teardown: stacks deleted": False}}) == "failed"
    assert run_e2e._live_cleanup_status(case, None) == "unverified"
    legacy = next(case for case in run_e2e.LIVE_CASES if case.live_runner == "legacy_a2a")
    assert run_e2e._live_cleanup_status(legacy, {"cleanup_status": "completed"}) == "completed"
    smoke = next(case for case in run_e2e.LIVE_CASES if case.live_runner == "smoke")
    assert run_e2e._live_cleanup_status(smoke, None) == "not-needed"


def test_live_requires_complete_credential_source_and_write_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_e2e.parse_args(["--suite", "live", "--credential-source-dir", str(tmp_path)])
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_e2e.parse_args(["--suite", "live", "--credential-source-dir", str(tmp_path)])
    args = run_e2e.parse_args(
        ["--suite", "live", "--credential-source-dir", str(tmp_path), "--allow-cloud-write"]
    )
    assert len(run_e2e.select_cases(args)) == len(run_e2e.LIVE_CASES)


def test_live_accepts_llm_only_source_with_cloud_helper(tmp_path: Path) -> None:
    for name in (".credentials.yml", "settings.yml"):
        (tmp_path / name).write_text("fixture", encoding="utf-8")
    helper = tmp_path / "helper.py"
    helper.write_text("", encoding="utf-8")
    args = run_e2e.parse_args([
        "--suite", "live", "--credential-source-dir", str(tmp_path),
        "--cloud-credential-helper", str(helper), "--cloud-credential-python", sys.executable,
        "--allow-cloud-write",
    ])
    assert len(run_e2e.select_cases(args)) == len(run_e2e.LIVE_CASES)


def test_cloud_helper_failure_is_classified_without_exposing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("import sys\nprint('secret-fixture', file=sys.stderr)\nsys.exit(1)\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", "settings.yml"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    report = tmp_path / "report"

    code = run_e2e.main([
        "--suite", "live", "--case", "a2a-recovery-scenario1", "--jobs", "1",
        "--run-dir", str(report), "--credential-source-dir", str(source),
        "--cloud-credential-helper", str(helper), "--allow-cloud-write",
    ])

    assert code == 1
    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    assert summary["cases"][0]["error"].startswith("cloud credential setup failed")
    assert "secret-fixture" not in (report / "report.md").read_text(encoding="utf-8")


def test_live_public_summary_drops_notes_error_and_paths() -> None:
    summary = {
        "case_id": "A01", "scenario": "example", "status": "failed", "cleanup_status": "completed",
        "checks": {"safe check": False, "unsafe key": "secret"},
        "notes": ["sensitive token"], "error": "sensitive token", "run_dir": "/private/path",
    }
    public = run_e2e._public_live_summary(summary)
    assert public == {
        "case_id": "A01", "scenario": "example", "status": "failed", "cleanup_status": "completed",
        "checks": {"safe check": False},
    }


def test_live_public_summary_keeps_safe_failure_location_only() -> None:
    public = run_e2e._public_live_summary({
        "status": "failed",
        "error": "RuntimeError: private provider response sk-fixture",
        "error_type": "RuntimeError",
        "error_site": "scripts/pipeline/e2e/selling_solution_first/run_scenarios.py:1752",
    })
    assert public is not None
    assert public["error_type"] == "RuntimeError"
    assert public["error_site"] == "scripts/pipeline/e2e/selling_solution_first/run_scenarios.py:1752"
    assert "sk-fixture" not in json.dumps(public)
    unsafe = run_e2e._public_live_summary({
        "error_type": "RuntimeError: sk-fixture",
        "error_site": "scripts/../../secrets.py:1",
    })
    assert unsafe is not None
    assert "error_type" not in unsafe
    assert "error_site" not in unsafe


def test_live_public_summary_keeps_only_bounded_headless_diagnostics() -> None:
    public = run_e2e._public_live_summary({
        "diagnostics": {
            "text_exit_code": 0,
            "text_output_length": 241,
            "text_has_vpc_marker": False,
            "text_output": "sk-fixture",
        },
    })
    assert public is not None
    assert public["diagnostics"] == {
        "text_exit_code": 0,
        "text_output_length": 241,
        "text_has_vpc_marker": False,
    }


def test_live_public_summary_keeps_only_fixed_rollback_cleanup_diagnostics() -> None:
    public = run_e2e._public_live_summary({
        "diagnostics": {
            "cleanup_turn_event_count": 12,
            "cleanup_delete_tool_use_count": 0,
            "cleanup_delete_error_code": "StackInOperation",
            "cleanup_delete_http_status": 409,
            "cleanup_delete_target_matches": True,
            "cleanup_delete_tool_kind": "aliyun_api",
            "cleanup_delete_error_kind": "resource_busy",
            "cleanup_prompt_active": True,
            "cleanup_first_ledger_status": "pending",
            "cleanup_first_ros_status": "CREATE_COMPLETE",
            "cleanup_turn_terminal_state": "TASK_STATE_INPUT_REQUIRED",
            "cleanup_target_id": "secret-stack-id",
            "cleanup_first_ledger_error": "secret provider response",
            "cleanup_first_snapshot_status": "secret provider response",
        },
    })
    assert public is not None
    assert public["diagnostics"] == {
        "cleanup_turn_event_count": 12,
        "cleanup_delete_tool_use_count": 0,
        "cleanup_delete_error_code": "StackInOperation",
        "cleanup_delete_http_status": 409,
        "cleanup_delete_target_matches": True,
        "cleanup_delete_tool_kind": "aliyun_api",
        "cleanup_delete_error_kind": "resource_busy",
        "cleanup_prompt_active": True,
        "cleanup_first_ledger_status": "pending",
        "cleanup_first_ros_status": "CREATE_COMPLETE",
        "cleanup_turn_terminal_state": "TASK_STATE_INPUT_REQUIRED",
    }


def test_live_public_summary_keeps_only_safe_cleanup_diagnostic() -> None:
    public = run_e2e._public_live_summary({
        "cleanup_diagnostic": {
            "error_type": "UnretryableError",
            "stage": "list_stacks",
            "sdk_code": "Throttling",
            "failure_count": 1,
            "remaining_count": 0,
            "stack_id": "sensitive-stack-id",
        },
    })
    assert public is not None
    assert public["cleanup_diagnostic"] == {
        "error_type": "UnretryableError",
        "stage": "list_stacks",
        "sdk_code": "Throttling",
        "failure_count": 1,
        "remaining_count": 0,
    }


def test_live_public_summary_keeps_only_known_a2a_states() -> None:
    public = run_e2e._public_live_summary({
        "a2a_states": ["TASK_STATE_WORKING", "TASK_STATE_FAILED", "TASK_STATE_PRIVATE_sk-fixture"],
        "a2a_phase": "next-turn",
        "a2a_event_count": 1,
        "a2a_text_present": True,
        "a2a_raw_line_count": 2,
        "a2a_response_content_type": "text/event-stream",
        "jsonrpc_error_code": -32602,
        "terminal_markers": ["resource_selection_resume_invalid", "sk-fixture"],
        "terminal_message_present": True,
        "control_state": {
            "present": True, "task_matches": True, "phase": "running",
            "release_ready": False, "input_handoff_ready": False,
            "execution_status": "working", "stream_available": True, "blocker_count": 2,
            "subprocess_tracking": True, "active_subprocess_tools": 0,
            "external_operation_count": 1, "revision_settled": True,
            "backup_status": "not_requested",
            "unsafe": "sk-fixture",
        },
    })
    assert public is not None
    assert public["a2a_states"] == ["TASK_STATE_WORKING", "TASK_STATE_FAILED"]
    assert public["a2a_phase"] == "next-turn"
    assert public["a2a_event_count"] == 1
    assert public["a2a_text_present"] is True
    assert public["a2a_raw_line_count"] == 2
    assert public["a2a_response_content_type"] == "text/event-stream"
    assert public["jsonrpc_error_code"] == -32602
    assert public["terminal_markers"] == ["resource_selection_resume_invalid"]
    assert public["terminal_message_present"] is True
    assert public["control_state"] == {
        "present": True, "task_matches": True, "phase": "running",
        "release_ready": False, "input_handoff_ready": False,
        "execution_status": "working", "stream_available": True, "blocker_count": 2,
        "subprocess_tracking": True, "active_subprocess_tools": 0,
        "external_operation_count": 1, "revision_settled": True,
        "backup_status": "not_requested",
    }
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_classifies_a2a_terminal_without_text() -> None:
    public = run_e2e._public_live_summary({
        "error": "RuntimeError: A2A task entered unexpected terminal state TASK_STATE_FAILED: "
        "ValueError: Rate limit exceeded for secret sk-fixture",
    })
    assert public is not None
    assert public["terminal_category"] == "rate_limit"
    assert public["terminal_exception"] == "ValueError"
    assert public["terminal_message_present"] is True
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_keeps_fixed_recovery_failure_stage_only() -> None:
    public = run_e2e._public_live_summary({
        "error_type": "TimeoutError",
        "error_site": "scripts/a2a/e2e/run_recovery_scenarios.py:1820",
        "failure_stage": "post_rollback_confirmation",
        "abort_reason": "private token sk-fixture",
    })
    assert public is not None
    assert public["error_type"] == "TimeoutError"
    assert public["failure_stage"] == "post_rollback_confirmation"
    assert "sk-fixture" not in json.dumps(public)
    assert "failure_stage" not in run_e2e._public_live_summary({"failure_stage": "sk-fixture"})
    cleanup_public = run_e2e._public_live_summary({"failure_stage": "first_stack_create"})
    assert cleanup_public is not None
    assert cleanup_public["failure_stage"] == "first_stack_create"


def test_live_public_summary_filters_runner_diagnostics() -> None:
    public = run_e2e._public_live_summary({
        "diagnostics": {
            "confirmation_event_count": 2,
            "unstructured_confirmation_count": 1,
            "image_confirmation_count": 1,
            "ros_deploy_event_count": 1,
            "public_tool_event_count": 3,
            "persisted_aliyun_public_tool_event_count": 0,
            "public_tool_names": ["aliyun_api", "sk-fixture"],
            "persisted_aliyun_public_tool_names": ["aliyun_api", "sk-fixture"],
            "persisted_aliyun_public_tool_name_categories": ["other_tool_name", "sk-fixture"],
            "persisted_aliyun_publicly_attributed": True,
            "repl_selection_submitted_count": 1,
            "repl_step1_stall_restarts": 1,
            "repl_step2_stall_restarts": 1,
            "repl_selection_image_retries": 1,
            "repl_normal_resume_reselections": 1,
            "repl_step2_attempt_count": 2,
            "repl_step2_tool_use_count": 4,
            "repl_step2_tool_use_names": ["ros_preview_template", "sk-fixture"],
            "repl_step_started_ids": ["solution_planning_and_selection", "sk-fixture"],
            "repl_first_rollback_input_intact": True,
            "a2a_pending_kinds": ["deployment_confirmation", "sk-fixture"],
            "repl_image_keys": ["initial", "sk-fixture"],
            "repl_failed_wait_phase": "rollback_image_input",
            "private": "sk-fixture",
        },
    })
    assert public is not None
    assert public["diagnostics"] == {
        "confirmation_event_count": 2,
        "unstructured_confirmation_count": 1,
        "image_confirmation_count": 1,
        "ros_deploy_event_count": 1,
        "public_tool_event_count": 3,
        "persisted_aliyun_public_tool_event_count": 0,
        "public_tool_names": ["aliyun_api"],
        "persisted_aliyun_public_tool_names": ["aliyun_api"],
        "persisted_aliyun_public_tool_name_categories": ["other_tool_name"],
        "persisted_aliyun_publicly_attributed": True,
        "repl_selection_submitted_count": 1,
        "repl_step1_stall_restarts": 1,
        "repl_step2_stall_restarts": 1,
        "repl_selection_image_retries": 1,
        "repl_normal_resume_reselections": 1,
        "repl_step2_attempt_count": 2,
        "repl_step2_tool_use_count": 4,
        "repl_step2_tool_use_names": ["ros_preview_template"],
        "repl_step_started_ids": ["solution_planning_and_selection"],
        "repl_first_rollback_input_intact": True,
        "a2a_pending_kinds": ["deployment_confirmation"],
        "repl_image_keys": ["initial"],
        "repl_failed_wait_phase": "rollback_image_input",
    }
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_bounds_privacy_and_startup_diagnostics():
    public = run_e2e._public_live_summary({'diagnostics': {
        'credential_audit_fields': ['context', 'private-secret'],
        'server_startup_error_types': ['OSError', 'private-secret'],
        'server_startup_process_alive': True, 'server_startup_port_in_use': False,
        'server_startup_return_code': -9, 'credential_audit_credential_kind': 'security_token',
    }})
    assert public['diagnostics'] == {
        'credential_audit_fields': ['context'], 'server_startup_error_types': ['OSError'],
        'server_startup_process_alive': True, 'server_startup_port_in_use': False,
        'server_startup_return_code': -9, 'credential_audit_credential_kind': 'security_token',
    }
    assert 'private-secret' not in json.dumps(public)


@pytest.mark.parametrize("value,expected", [(2, 2), (-1, None), (10001, None), (True, None), ("private", None)])
def test_repl_tool_checkpoint_question_diagnostic_keeps_only_bounded_count(value, expected):
    public = run_e2e._public_live_summary({'diagnostics': {'repl_tool_checkpoint_parameter_asks': value}})
    assert public.get('diagnostics', {}).get('repl_tool_checkpoint_parameter_asks') == expected


def test_agui_lead_in_diagnostics_keep_only_bounded_counts():
    summary = {'scenario': 'pipeline-direct-input', 'leadInDeniedLocalFilePermissions': 1,
               'leadInCandidateTurns': 2, 'leadInQuestionTurns': 0, 'leadInRawQuestion': 'private-text',
               'aguiStreamTrace': ['RUN_STARTED', 'private-text', {'secret': 'private-text'},
                                   'pipeline:step_failed', 'RUN_ERROR:A2A_EXECUTION_FAILED']}
    public = run_e2e._public_live_summary(summary)
    for key in ('leadInDeniedLocalFilePermissions', 'leadInCandidateTurns', 'leadInQuestionTurns'):
        assert public[key] == summary[key]
    assert 'private-text' not in json.dumps(public)
    assert public['aguiStreamTrace'] == ['RUN_STARTED', 'pipeline:step_failed', 'RUN_ERROR:A2A_EXECUTION_FAILED']
    summary['leadInDeniedLocalFilePermissions'] = 'private-path'
    assert 'leadInDeniedLocalFilePermissions' not in run_e2e._public_live_summary(summary)


def test_live_public_summary_extracts_only_safe_chinese_terminal_terms() -> None:
    public = run_e2e._public_live_summary({
        "error": "RuntimeError: A2A task entered unexpected terminal state TASK_STATE_FAILED: "
        "恢复会话失败，凭证 sk-fixture 不可用",
    })
    assert public is not None
    assert set(public["terminal_terms"]) == {"恢复", "会话", "失败", "凭证", "不可用"}
    assert "sk-fixture" not in json.dumps(public)


def test_live_public_summary_recognizes_fixed_snake_case_code() -> None:
    public = run_e2e._public_live_summary({
        "error": "RuntimeError: A2A task entered unexpected terminal state TASK_STATE_FAILED: "
        "pipeline_identity_mismatch; private value sk-fixture",
    })
    assert public is not None
    assert public["terminal_code"] == "pipeline_identity_mismatch"
    assert {"pipeline", "identity", "mismatch"} <= set(public["terminal_terms"])
    assert "sk-fixture" not in json.dumps(public)


def test_live_a2a_terminal_evidence_keeps_only_fixed_fields(tmp_path: Path) -> None:
    event = {
        "metadata": {"iac_code": {"pipeline": {
            "eventType": "pipeline_failed",
            "data": {
                "errorSummary": "ValueError: Rate limit exceeded; token=sk-fixture",
                "errorDetails": {"type": "ValueError", "traceback": "secret fixture"},
            },
        }}},
    }
    (tmp_path / "failed.events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    evidence = run_e2e._live_a2a_terminal_evidence(tmp_path)

    assert evidence == {
        "pipeline_failed_event": "observed",
        "terminal_inner_type": "ValueError",
        "terminal_category": "rate_limit",
        "terminal_terms": ["error"],
        "pipeline_events": ["pipeline_failed"],
    }
    assert "sk-fixture" not in json.dumps(evidence)


def test_live_a2a_terminal_evidence_reads_persistent_journal(tmp_path: Path) -> None:
    journal = tmp_path / "config" / "projects" / "session" / "pipeline" / "a2a-events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({
        "events": [
            {"eventType": "step_started"},
            {"eventType": "pipeline_failed", "data": {
                "errorSummary": "TimeoutError: private provider payload sk-fixture",
                "errorDetails": {"type": "TimeoutError"},
            }},
        ],
    }) + "\n", encoding="utf-8")

    evidence = run_e2e._live_a2a_terminal_evidence(tmp_path)

    assert evidence == {
        "pipeline_failed_event": "observed",
        "terminal_inner_type": "TimeoutError",
        "terminal_category": "timeout",
        "terminal_terms": ["provider", "error"],
        "pipeline_events": ["step_started", "pipeline_failed"],
    }
    assert "sk-fixture" not in json.dumps(evidence)


def test_live_a2a_terminal_evidence_reports_step_failure_without_raw_error(tmp_path: Path) -> None:
    event = {
        "metadata": {"iac_code": {"pipeline": {
            "eventType": "step_failed",
            "step": {"id": "materialize_selected_candidate"},
            "data": {
                "errorSummary": "TimeoutError: provider timed out; token=sk-fixture",
                "errorDetails": {"type": "TimeoutError", "traceback": "private fixture"},
            },
        }}},
    }
    (tmp_path / "failed.events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    evidence = run_e2e._live_a2a_terminal_evidence(tmp_path)

    assert evidence == {
        "step_failed_event": "observed",
        "step_failure_step": "materialize_selected_candidate",
        "step_failure_inner_type": "TimeoutError",
        "step_failure_category": "timeout",
        "step_failure_terms": ["provider", "error"],
        "pipeline_events": ["step_failed"],
    }
    assert "sk-fixture" not in json.dumps(evidence)


def test_live_a2a_progress_evidence_ignores_untrusted_event_names(tmp_path: Path) -> None:
    events = [
        {"metadata": {"iac_code": {"pipeline": {"eventType": "input_required"}}}},
        {"metadata": {"iac_code": {"pipeline": {"eventType": "private-token-sk-fixture"}}}},
    ]
    (tmp_path / "turn.events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    assert run_e2e._live_a2a_terminal_evidence(tmp_path) == {"pipeline_events": ["input_required"]}


def test_live_public_summary_keeps_only_safe_watchdog_fields() -> None:
    summary = {
        "passed": False,
        "watchdog": {
            "state": "waiting_for_input", "confidence": 0.93,
            "waitingFor": "pipeline completed", "elapsedSeconds": 125.33,
            "action": "early_abort", "cue": "ask_question", "raw": "secret-fixture-value",
        },
        "progress": {
            "candidate_selection_ready": 2, "user_input_received": 1,
            "ros_deploy_create_failure": 0, "stack_progress_create_failed": 0,
            "ros_deploy_used": 1, "pipeline_completed_early_exit": 1,
            "stack_progress_create_complete": 1, "cleanup_ledger_found": 0,
            "cleanup_failure_route_conflict": 1,
            "cleanup_failure_route_conflict_after_rollback": 0,
            "private-token-sk-fixture": 9, "step_started": True,
        },
    }

    public = run_e2e._public_live_summary(summary)

    assert public is not None
    assert public["watchdog"] == {
        "state": "waiting_for_input", "waitingFor": "pipeline completed",
        "elapsedSeconds": 125.3, "action": "early_abort", "cue": "ask_question",
    }
    assert public["progress"] == {
        "candidate_selection_ready": 2, "user_input_received": 1,
        "ros_deploy_create_failure": 0, "stack_progress_create_failed": 0,
        "ros_deploy_used": 1, "pipeline_completed_early_exit": 1,
        "stack_progress_create_complete": 1, "cleanup_ledger_found": 0,
        "cleanup_failure_route_conflict": 1,
        "cleanup_failure_route_conflict_after_rollback": 0,
    }
    assert "secret-fixture-value" not in json.dumps(public)
    assert run_e2e._public_live_summary({
        "watchdog": {**summary["watchdog"], "waitingFor": "secret: sk-fixture"}
    }) == {
        "case_id": None, "scenario": None, "status": "failed", "cleanup_status": None, "checks": {},
    }


def test_question_contract_diagnostics_drop_unapproved_labels_and_private_values():
    public = run_e2e._public_live_summary({'diagnostics': {
        'question_driver_question_subjects': ['scale', 'private-question'],
        'question_driver_available_fact_keys': ['goal', 'private-fact'],
        'question_driver_selected_fact_keys': ['purpose', 'private-fact'],
        'question_driver_option_count': 2,
        'question_driver_option_selected': False,
        'question_driver_free_text_allowed': True,
        'question_driver_fixture_cidr_synced': True,
        'question_driver_review_count': 1,
        'question_driver_missing_fields': ['scale', 'budget', 'cloud_vendor', 'private-label'],
        'question_driver_review': {'raw': 'private-label'},
        'selector_vpc_matches_selected': False,
    }})
    assert public['diagnostics'] == {
        'question_driver_question_subjects': ['scale'], 'question_driver_available_fact_keys': ['goal'],
        'question_driver_selected_fact_keys': ['purpose'], 'question_driver_option_count': 2,
        'question_driver_option_selected': False, 'question_driver_free_text_allowed': True,
        'question_driver_fixture_cidr_synced': True,
        'selector_vpc_matches_selected': False,
        'question_driver_review_count': 1,
        'question_driver_missing_fields': ['budget', 'cloud_vendor', 'scale'],
    }
    assert 'private' not in json.dumps(public)


def test_question_diagnostics_export_fixed_categories_without_history_or_model_text():
    public = run_e2e._public_live_summary({'diagnostics': {
        'question_driver_supplement_count': 2, 'question_driver_goal_reset_count': 1,
        'question_driver_missing_fields': ['vpc_id', 'sk-private-secret', {'raw': 'private'}],
        'question_conversation': [{'answer': 'private cloud fact'}],
    }, 'watchdog': {'state': 'waiting_for_input', 'action': 'observe', 'waitingFor': 'pipeline completed',
                   'elapsedSeconds': 120, 'inputKind': 'clarification', 'suggestedHandler': 'question_driver'}})
    assert public['diagnostics'] == {'question_driver_supplement_count': 2, 'question_driver_goal_reset_count': 1,
                                     'question_driver_missing_fields': ['vpc_id']}
    assert public['watchdog']['inputKind'] == 'clarification'
    assert public['watchdog']['suggestedHandler'] == 'question_driver'
    assert 'private' not in json.dumps(public)
    result = {'status': 'failed', 'error': '', 'cleanupStatus': 'completed', 'summary': public,
              'failedChecks': [], 'live': True, 'notes': []}
    assert '缺少用例事实：vpc_id' in run_e2e._reason(result)


def test_live_audit_note_allowlist_excludes_provider_data() -> None:
    assert run_e2e.SAFE_LIVE_AUDIT_NOTE.fullmatch(
        "credential audit: source=cloud; location=other; suffix=json; artifact=a2a_task"
    )
    assert not run_e2e.SAFE_LIVE_AUDIT_NOTE.fullmatch(
        "credential audit: source=cloud; location=other; suffix=json; artifact=private-token"
    )
    assert run_e2e.SAFE_LIVE_AUDIT_NOTE.fullmatch(
        "credential audit: source=cloud; location=logs; suffix=log"
    )
    assert not run_e2e.SAFE_LIVE_AUDIT_NOTE.fullmatch(
        "credential audit: source=cloud; location=logs; suffix=log; secret=unit-secret-value"
    )


def test_nested_contract_failure_details_are_reported() -> None:
    checks, notes = run_e2e._failure_details(
        {"passed": False, "scenarios": [{"checks": {"provider request observed": False}, "notes": ["missing request"]}]}
    )
    assert checks == ["provider request observed"]
    assert notes == ["missing request"]


def test_child_environment_removes_cloud_and_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALIYUN_ACCESS_KEY_ID", "fake-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-secret")
    monkeypatch.setenv("IAC_CODE_API_KEY", "fake-secret")
    monkeypatch.setenv("AKLESS_BOOTSTRAP_TOKEN", "fake-secret")
    monkeypatch.setenv("IAC_CODE_E2E_PROVIDER_CAPTURE", "inherited-fixture")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://telemetry.example.com")
    user_id = "iac_user_e2e_" + "a" * 32
    env = run_e2e._case_env(tmp_path, run_e2e.EXECUTION_CASES[0], user_id)
    assert "ALIYUN_ACCESS_KEY_ID" not in env
    assert "OPENAI_API_KEY" not in env
    assert "IAC_CODE_API_KEY" not in env
    assert "AKLESS_BOOTSTRAP_TOKEN" not in env
    assert "IAC_CODE_E2E_PROVIDER_CAPTURE" not in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert env["IAC_CODE_CONFIG_DIR"] == str(tmp_path / "config")
    assert env["IAC_CODE_TELEMETRY_E2E_USER_ID"] == user_id
    assert "IAC_CODE_TELEMETRY_LOCAL_ONLY" not in env
    local_env = run_e2e._case_env(tmp_path, run_e2e.FAST_CASES[0], user_id)
    assert local_env["IAC_CODE_TELEMETRY_LOCAL_ONLY"] == "1"
    for case in run_e2e.LIVE_CASES:
        live_env = run_e2e._case_env(tmp_path, case, user_id)
        assert (live_env.get("IAC_CODE_TELEMETRY_LOCAL_ONLY") == "1") == (
            case.name in run_e2e.LOCAL_TELEMETRY_LIVE_CASES
        )


def test_e2e_settings_id_is_preserved_or_generated(tmp_path: Path) -> None:
    settings = tmp_path / "settings.yml"
    settings.write_text('{"userID": "iac_user_regular", "activeProvider": "dashscope"}', encoding="utf-8")
    generated = run_e2e._prepare_e2e_user_id(settings)
    assert generated.startswith("iac_user_e2e_")
    assert generated in settings.read_text(encoding="utf-8")
    assert run_e2e._prepare_e2e_user_id(settings) == generated


def test_timeout_writes_failure_report_without_hanging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("hung", "hang.py", (), 1, "full")
    result = run_e2e.run_case(case, tmp_path / "report")
    assert result["status"] == "timeout"
    assert result["durationSeconds"] < 8
    run_e2e._write_reports(tmp_path / "report", [result], result["durationSeconds"])
    summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert summary["failedCount"] == 1
    assert "硬超时" in (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert ET.parse(tmp_path / "report" / "junit.xml").find(".//failure") is not None
    result["live"] = True
    result["cleanupStatus"] = "unverified"
    assert "清理结果未验证" in run_e2e._reason(result)


def test_summary_failure_keeps_failed_checks_and_log_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fail.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "summary = {'passed': False, 'checks': {'step one': False}}\n"
        "(d / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')\n"
        "print('fixture failure')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("failed", "fail.py", (), 5, "full")
    result = run_e2e.run_case(case, tmp_path / "report")
    assert result["status"] == "failed"
    assert result["failedChecks"] == ["step one"]
    run_e2e._write_reports(tmp_path / "report", [result], result["durationSeconds"])
    page = (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    markdown = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    machine_summary = json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))
    assert "runs/failed/stdout.log" in page
    assert "step one" in page
    assert "· 失败 ·" in page
    assert "| 失败 |" in markdown
    assert machine_summary["cases"][0]["status"] == "failed"
    assert sys.executable in result["command"]
    assert os.path.isfile(tmp_path / "report" / "runs" / "failed" / "ci-result.json")


@pytest.mark.parametrize("summary", [
    {"passed": True, "checks": {"required acceptance": False}},
    {"status": "passed", "checks": {"required acceptance": False}},
    {"passed": True, "scenarios": [{"checks": {"required acceptance": False}}]},
])
@pytest.mark.parametrize("suite", ["full", "live"])
def test_failed_acceptance_overrides_success_summary_and_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, summary: dict, suite: str,
) -> None:
    summary = {**summary, "cleanup_status": "completed"}
    script = tmp_path / "contradiction.py"
    script.write_text(
        "import pathlib, sys\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        f"(d / 'summary.json').write_text({json.dumps(summary)!r}, encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("contradiction", script.name, (), 5, suite, live_runner="legacy_a2a")
    report = tmp_path / "report"
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("{}", encoding="utf-8")
    result = run_e2e.run_case(case, report, source if suite == "live" else None)

    assert result["returnCode"] == 0
    assert result["status"] == "failed"
    assert result["failedChecks"] == (
        ["场景验收检查失败；查看 CI 作业日志"]
        if suite == "live" and "scenarios" in summary else ["required acceptance"]
    )
    run_e2e._write_reports(report, [result], result["durationSeconds"])
    assert json.loads((report / "summary.json").read_text(encoding="utf-8"))["failedCount"] == 1
    assert ET.parse(report / "junit.xml").find(".//failure") is not None


@pytest.mark.parametrize("cleanup_status", ["failed", "unverified", "skipped", "", None, "completed", "not-needed"])
def test_live_success_requires_verified_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_status: str | None,
) -> None:
    summary = {"passed": True, "checks": {"required acceptance": True}}
    if cleanup_status is not None:
        summary["cleanup_status"] = cleanup_status
    script = tmp_path / "cleanup.py"
    script.write_text(
        "import pathlib, sys\n"
        "d = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        f"(d / 'summary.json').write_text({json.dumps(summary)!r}, encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("cleanup", script.name, (), 5, "live", live_runner="legacy_a2a")
    report = tmp_path / "report"
    result = run_e2e.run_case(case, report, source)

    expected = "passed" if cleanup_status in {"completed", "not-needed"} else "failed"
    assert result["returnCode"] == 0
    assert result["status"] == expected
    assert result["cleanupStatus"] == (cleanup_status or "unverified")
    if expected == "failed":
        assert "清理失败" in run_e2e._reason(result) or "清理结果未验证" in run_e2e._reason(result)
    run_e2e._write_reports(report, [result], result["durationSeconds"])
    assert json.loads((report / "summary.json").read_text(encoding="utf-8"))["failedCount"] == int(expected == "failed")
    assert (ET.parse(report / "junit.xml").find(".//failure") is not None) == (expected == "failed")


def test_unexpected_case_exception_still_writes_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object) -> dict[str, object]:
        raise ValueError("broken fixture")

    monkeypatch.setattr(run_e2e, "run_case", fail)
    assert run_e2e.main(["--suite", "fast", "--run-dir", str(tmp_path)]) == 1
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["failedCount"] == len(run_e2e.FAST_CASES)
    assert (tmp_path / "report.md").is_file()


@pytest.mark.parametrize("runner", ["selector", "repl", "agui_selector"])
def test_live_adapter_uses_isolated_credentials_and_sanitized_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    script = tmp_path / "fake_live.py"
    script.write_text(
        "import argparse, json, os, yaml\n"
        "from pathlib import Path\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--run-dir', type=Path, required=True)\n"
        "p.add_argument('--source-config-dir', type=Path)\n"
        "args, _ = p.parse_known_args()\n"
        "if args.source_config_dir is None:\n"
        "    args.source_config_dir = Path(os.environ['IAC_CODE_CONFIG_DIR'])\n"
        "    assert '--provider' not in _ and '--python' not in _\n"
        "if args.run_dir.name.startswith('scenario-'):\n"
        "    args.run_dir.mkdir(parents=True, exist_ok=False)\n"
        "assert (args.source_config_dir / '.credentials.yml').read_text(encoding='utf-8') == 'fixture-secret'\n"
        "assert yaml.safe_load((args.source_config_dir / 'settings.yml').read_text("
        "encoding='utf-8'))['userID'] == os.environ['IAC_CODE_TELEMETRY_E2E_USER_ID']\n"
        "(args.run_dir / 'summary.json').write_text("
        "json.dumps({'passed': True, 'checks': {'ok': True, 'teardown: stacks deleted': True}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("fixture-secret", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("selector-smoke" if runner == "selector" else "repl-smoke", "fake_live.py", (),
                        5, "live", live_runner=runner)

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "passed"
    assert result["command"] == [case.name]
    assert "fixture-secret" not in json.dumps(result)
    assert (tmp_path / "report" / "runs" / case.name / "config" / ".credentials.yml").is_file()
    if runner == "repl":
        scenarios = list((tmp_path / "report" / "runs" / case.name).glob("scenario-*"))
        assert len(scenarios) == 1
        assert len(scenarios[0].name.removeprefix("scenario-")) == 32


def test_smoke_adapter_has_model_config_but_no_cloud_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "smoke.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "run_dir = Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "config = Path(os.environ['IAC_CODE_CONFIG_DIR'])\n"
        "assert (config / '.credentials.yml').read_text(encoding='utf-8') == 'model-fixture'\n"
        "assert (config / 'settings.yml').is_file()\n"
        "assert not (config / '.cloud-credentials.yml').exists()\n"
        "assert Path(os.environ['HOME']).is_relative_to(run_dir)\n"
        "assert Path(os.environ['USERPROFILE']).is_relative_to(run_dir)\n"
        "assert '--allow-real-cloud' not in sys.argv\n"
        "(run_dir / 'summary.json').write_text("
        "json.dumps({'passed': True, 'checks': {'ok': True}}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("model-fixture", encoding="utf-8")
    (source / ".cloud-credentials.yml").write_text("cloud-fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("smoke-vpc-fixture", "smoke.py", (), 5, "live", live_runner="smoke")

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "passed"
    assert result["cleanupStatus"] == "not-needed"
    assert "cloud-fixture" not in json.dumps(result)


@pytest.mark.parametrize("runner", ["repl", "selling", "canary", "agui_selector"])
def test_cloud_helper_generates_per_case_sts_without_leaking_bootstrap_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import os, pathlib, sys\n"
        "assert sys.argv[1] == 'cloud'\n"
        "assert os.environ['AKLESS_BOOTSTRAP_TOKEN'] == 'bootstrap-fixture'\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "path.write_text('temporary-sts', encoding='utf-8')\n",
        encoding="utf-8",
    )
    script = tmp_path / "live.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "source = pathlib.Path(sys.argv[sys.argv.index('--source-config-dir') + 1])\n"
        if runner in {"repl", "canary"} else
        "import json, os, pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "source = pathlib.Path(sys.argv[sys.argv.index('--credential-source-dir') + 1])\n"
        if runner == "selling" else
        "import json, os, pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "source = pathlib.Path(os.environ['IAC_CODE_CONFIG_DIR'])\n",
        encoding="utf-8",
    )
    with script.open("a", encoding="utf-8") as stream:
        stream.write(
            "assert 'AKLESS_BOOTSTRAP_TOKEN' not in os.environ\n"
            "assert 'DASHSCOPE_API_KEY' not in os.environ\n"
            "assert (source / '.cloud-credentials.yml').read_text(encoding='utf-8') == 'temporary-sts'\n"
            "run_dir.mkdir(parents=True, exist_ok=True)\n"
            "(run_dir / 'summary.json').write_text(json.dumps({'passed': True, 'cleanup_status': 'completed', "
            "'checks': {'teardown: stacks deleted': True}}), encoding='utf-8')\n"
        )
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("model-key", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setenv("AKLESS_BOOTSTRAP_TOKEN", "bootstrap-fixture")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "model-key")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case("akless-" + runner, "live.py", (), 5, "live", live_runner=runner)

    result = run_e2e.run_case(case, tmp_path / "report", source, helper, Path(sys.executable))

    assert result["status"] == "passed"
    assert "bootstrap-fixture" not in json.dumps(result)
    if runner in {"selling", "canary"}:
        assert (tmp_path / "report" / "runs" / case.name / "credential-source" / ".cloud-credentials.yml").is_file()


@pytest.mark.parametrize("runner", ["legacy_a2a", "agui_selector"])
def test_long_live_case_refreshes_cloud_file_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "count = path.parent / 'refresh-count'\n"
        "value = int(count.read_text(encoding='utf-8')) + 1 if count.exists() else 1\n"
        "count.write_text(str(value), encoding='utf-8')\n"
        "path.write_text('sts-' + str(value), encoding='utf-8')\n",
        encoding="utf-8",
    )
    script = tmp_path / "long.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "run_dir.mkdir(parents=True, exist_ok=True)\n"
        "time.sleep(0.4)\n"
        "(run_dir / 'summary.json').write_text(json.dumps({'passed': True, 'cleanup_status': 'completed'}), "
        "encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", "settings.yml"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_e2e, "CLOUD_REFRESH_SECONDS", 0.1)
    case = run_e2e.Case("akless-refresh", "long.py", (), 5, "live", live_runner=runner)

    result = run_e2e.run_case(case, tmp_path / "report", source, helper)

    assert result["status"] == "passed"
    count = tmp_path / "report" / "runs" / case.name / "config" / "refresh-count"
    assert int(count.read_text(encoding="utf-8")) >= 2


def test_legacy_a2a_hard_timeout_starts_independent_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "hang_with_stack.py"
    script.write_text(
        "import pathlib, sys, time\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "(run_dir / 'owned-stacks.json').write_text('fixture', encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    cleanup = tmp_path / "scripts" / "a2a" / "e2e" / "cleanup_owned_stacks.py"
    cleanup.parent.mkdir(parents=True)
    cleanup.write_text(
        "import pathlib, sys\n"
        "run_dir = pathlib.Path(sys.argv[sys.argv.index('--run-dir') + 1])\n"
        "(run_dir / 'cleanup-called').write_text('yes', encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    for name in (".credentials.yml", ".cloud-credentials.yml", "settings.yml"):
        (source / name).write_text("fixture", encoding="utf-8")
    (source / "settings.yml").write_text('{"activeProvider": "dashscope"}', encoding="utf-8")
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    case = run_e2e.Case(
        "legacy-timeout", "hang_with_stack.py", (), 1, "live", cloud_write=True,
        cleanup_grace=0, live_runner="legacy_a2a",
    )

    result = run_e2e.run_case(case, tmp_path / "report", source)

    assert result["status"] == "timeout"
    assert result["cleanupStatus"] == "completed"
    assert (tmp_path / "report" / "runs" / case.name / "cleanup-called").read_text(encoding="utf-8") == "yes"


def test_local_failure_facts_expose_only_fixed_types_and_repo_locations(tmp_path):
    runner = run_e2e
    (tmp_path / 'server-1.log').write_text(
        'Traceback (most recent call last):\n'
        '  File "/private/worker/src/iac_code/providers/example.py", line 123, in request\n'
        'ValueError: sk-real-secret-token /private/user/home response-body\n', encoding="utf-8")
    evidence = runner._local_failure_facts(tmp_path)
    assert evidence['local_error_types'] == ['ValueError']
    assert evidence['local_error_sites'] == ['src/iac_code/providers/example.py:123']
    assert 'sk-real' not in json.dumps(evidence)
    assert '/private' not in json.dumps(evidence)


def test_candidate_enrichment_failure_diagnostic_exports_category_only(tmp_path):
    from scripts.ci.live_diagnostics import _completion_failure_facts
    session = tmp_path / 'transcripts' / 'attempt' / 'session.jsonl'
    session.parent.mkdir(parents=True)
    rows = [{'content': [{'type': 'tool_use', 'name': 'complete_step', 'id': 'call'}]},
            {'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'is_error': True,
                          'content': 'selected completion is blocked because a new candidate batch was generated; '
                                     'private resource contents must not be exported'}]}]
    session.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    evidence = _completion_failure_facts(tmp_path)
    assert evidence['completion_error_codes'] == {'selected_new_batch': 1}
    assert 'private' not in json.dumps(evidence)


def test_cloud_failure_diagnostics_require_real_correlated_error_and_hide_payload(tmp_path):
    from scripts.ci.live_diagnostics import _cloud_tool_failure_facts

    session = tmp_path / 'transcripts' / 'attempt' / 'session.jsonl'
    session.parent.mkdir(parents=True)
    rows = [
        {'content': [{'type': 'text', 'text': 'ros_deploy CREATE_FAILED RouteConflict'}]},
        {'content': [{'type': 'tool_result', 'tool_use_id': 'unknown', 'is_error': True,
                      'content': 'RouteConflict unrelated'}]},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'deploy', 'name': 'ros_deploy'}]},
        {'content': [{'type': 'tool_result', 'tool_use_id': 'deploy', 'is_error': False,
                      'content': 'CREATE_FAILED is a schema example'}]},
        {'content': [{'type': 'tool_result', 'tool_use_id': 'deploy', 'is_error': True,
                      'content': 'CREATE_FAILED RouteConflict sk-private-secret stack-private-id'}]},
    ]
    session.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = _cloud_tool_failure_facts(tmp_path, None)
    assert facts == {
        'cloud_tool_error_categories': {'cidr_conflict': 1, 'create_failed': 1},
        'cloud_tool_error_by_tool': {'ros_deploy:cidr_conflict': 1, 'ros_deploy:create_failed': 1},
        'cloud_tool_error_parameter_fields': {},
    }
    assert 'private' not in json.dumps(facts)


def test_candidate_lifecycle_failure_diagnostic_exports_only_fixed_resource_actions(tmp_path):
    from scripts.ci.live_diagnostics import _completion_failure_facts

    session = tmp_path / 'transcripts/a/session.jsonl'
    session.parent.mkdir(parents=True)
    rows = [{'content': [{'type': 'tool_use', 'name': 'complete_step', 'id': 'call'}]},
            {'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'is_error': True,
                          'content': 'candidates[0].resource_intents must preserve authoritative intent lifecycle: '
                                     'VPC:use_existing, ECS:forbid, private_resource_name:create; '
                                     'submit a corrected candidate batch and details sk-private'}]}]
    session.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    facts = _completion_failure_facts(tmp_path)
    assert facts['candidate_missing_lifecycles'] == {'vpc:use_existing': 1, 'ecs:forbid': 1}
    assert 'private' not in json.dumps(facts)


def test_cloud_ownership_hashes_correlate_case_and_audit_without_exposing_identity(tmp_path):
    import hashlib

    from scripts.ci.live_diagnostics import collect_live_diagnostics

    resources = [{'stackId': 'private-stack-id', 'stackName': 'private-stack-name'}]
    (tmp_path / 'cloud-resources.json').write_text(json.dumps(resources), encoding='utf-8')
    (tmp_path / 'owned-stack-names.json').write_text(
        json.dumps({'stackNames': ['private-stack-name']}), encoding='utf-8')
    facts = collect_live_diagnostics(tmp_path, {})
    assert facts['cloud_stack_id_hashes'] == [hashlib.sha256(b'private-stack-id').hexdigest()]
    assert facts['cloud_stack_name_hashes'] == facts['owned_stack_name_hashes']
    assert 'private' not in json.dumps(facts)


def test_2c4g_diagnostics_keep_only_fixed_rejection_categories():
    public = run_e2e._public_live_summary({'diagnostics': {
        '2c4g_constraint_categories': {'status:satisfied': 2, 'actual_value:2': 1,
                                     'actual_value:private': 1, 'parameter_binding:InstanceType': 2},
        '2c4g_sdk_probe_category': 'incomplete_model_verification',
        '2c4g_sdk_actual_types_correct': True,
        'private_sku': 'private-value',
    }})
    assert public['diagnostics']['2c4g_constraint_categories'] == {
        'status:satisfied': 2, 'actual_value:2': 1, 'parameter_binding:InstanceType': 2}
    assert public['diagnostics']['2c4g_sdk_probe_category'] == 'incomplete_model_verification'
    assert public['diagnostics']['2c4g_sdk_actual_types_correct'] is True
    assert 'private' not in json.dumps(public)


def test_pre_teardown_diagnostic_never_exports_unprojected_control_fields():
    public = run_e2e._public_live_summary({'diagnostics': {
        'pre_teardown_control_state': {'phase': 'running', 'execution_status': 'working',
            'present': True, 'secret': 'private token', 'contextId': 'private-id'},
        'http_status_code': 500,
    }})
    assert public['diagnostics']['pre_teardown_control_state'] == {
        'phase': 'running', 'execution_status': 'working', 'present': True,
    }
    assert public['diagnostics']['http_status_code'] == 500
    assert 'private' not in json.dumps(public)


def test_isolated_image_case_preserves_only_the_nonsecret_font_path(tmp_path, monkeypatch):
    font = tmp_path / 'font.otf'
    font.write_bytes(b'fake-font')
    monkeypatch.setenv('IAC_CODE_E2E_FONT_PATH', str(font))
    monkeypatch.setenv('IAC_CODE_API_KEY', 'fake-secret')
    monkeypatch.setenv('IAC_CODE_TELEMETRY_E2E_USER_ID', 'regular-user')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'https://inherited.invalid')
    case = next(c for c in run_e2e.LIVE_CASES if c.name == 'ssf-a2a-image-initial-selection')
    identity = 'iac_user_e2e_' + 'b' * 32
    env = run_e2e._case_env(tmp_path / 'case', case, identity)
    assert env['IAC_CODE_E2E_FONT_PATH'] == str(font)
    assert 'IAC_CODE_API_KEY' not in env and 'OTEL_EXPORTER_OTLP_ENDPOINT' not in env
    assert env['IAC_CODE_TELEMETRY_E2E_USER_ID'] == identity
    assert 'IAC_CODE_TELEMETRY_LOCAL_ONLY' not in env


def test_golden_diagnostic_projection_preserves_only_boolean_evidence():
    public = run_e2e._public_live_summary({'diagnostics': {
        'golden_read_path_seen': True, 'golden_read_alias_seen': False,
        'golden_tagged_write_seen': 'private body', 'golden_tagged_completion_seen': True,
        'golden_bash_path_seen': False, 'golden_path': '/private/path',
    }})
    assert public['diagnostics'] == {'golden_read_path_seen': True, 'golden_read_alias_seen': False,
        'golden_tagged_completion_seen': True, 'golden_bash_path_seen': False}
    assert 'private' not in json.dumps(public)


def test_golden_structure_projection_drops_bodies_and_non_boolean_facts():
    public = run_e2e._public_live_summary({'diagnostics': {
        'golden_generated_template_parsed': True,
        'golden_generated_metadata_tag_seen': False,
        'golden_generated_bootstrap_matches': 'private-bootstrap-content',
        'golden_generated_template': 'private-template',
    }})
    assert public['diagnostics'] == {
        'golden_generated_template_parsed': True, 'golden_generated_metadata_tag_seen': False,
    }
    assert 'private' not in json.dumps(public)


@pytest.mark.parametrize(('value', 'expected'), [(1, 1), (True, None), ('private-value', None)])
def test_rollback_handoff_counter_is_a_public_numeric_fact(value, expected):
    public = run_e2e._public_live_summary({'diagnostics': {'rollback_planning_pending_input_count': value}})
    assert public.get('diagnostics', {}).get('rollback_planning_pending_input_count') == expected
    assert 'private' not in json.dumps(public)


def test_target_and_question_shape_diagnostics_export_only_fixed_fields():
    public = run_e2e._public_live_summary({'diagnostics': {
        'final_target_handoff_present': True,
        'final_target_context_present': True,
        'final_target_selected_plan_present': True,
        'final_target_template_body_present': True,
        'final_target_selected_plan_fields': ['deployment_parameters', 'selected_candidate_result', 'private-secret'],
        'final_target_candidate_result_fields': ['template', 'private-secret'],
        'question_driver_name_subject_present': True,
        'question_driver_existing_resource_requested': True,
        'question_driver_new_resource_requested': False,
        'question_driver_question_resource_kinds': ['oss', 'private-secret'],
    }})
    diagnostics = public['diagnostics']
    assert diagnostics['final_target_selected_plan_fields'] == ['deployment_parameters', 'selected_candidate_result']
    assert diagnostics['final_target_candidate_result_fields'] == ['template']
    assert diagnostics['final_target_template_body_present'] is True
    assert diagnostics['question_driver_question_resource_kinds'] == ['oss']
    assert diagnostics['question_driver_existing_resource_requested'] is True
    assert diagnostics['question_driver_new_resource_requested'] is False
    assert 'private' not in json.dumps(public)


def test_public_live_summary_keeps_only_bounded_cidr_hash_diagnostics():
    digest = 'a' * 64
    public = run_e2e._public_live_summary({'diagnostics': {
        'repl_requested_adjusted_cidr_hash': digest,
        'repl_initial_preview_cidr_hashes': [digest, 'private-network', 'b' * 64],
        'repl_adjustment_native_actual_cidr_hashes': [digest, 'private-network'],
        'private': 'private-secret',
    }})
    assert public['diagnostics']['repl_requested_adjusted_cidr_hash'] == digest
    assert public['diagnostics']['repl_initial_preview_cidr_hashes'] == [digest, 'b' * 64]
    assert public['diagnostics']['repl_adjustment_native_actual_cidr_hashes'] == [digest]
    assert 'private' not in json.dumps(public)
    public = run_e2e._public_live_summary({'diagnostics': {
        'repl_requested_adjusted_cidr_hash': 'private-cidr',
        'repl_initial_preview_cidr_hashes': [digest] * 20 + ['b' * 64],
    }})
    assert 'repl_requested_adjusted_cidr_hash' not in public['diagnostics']
    assert public['diagnostics']['repl_initial_preview_cidr_hashes'] == [digest]
def test_public_live_summary_keeps_fixed_native_target_counts_without_cloud_bodies():
    public = run_e2e._public_live_summary({'diagnostics': {
        'final_target_native_resources_inspected': True,
        'final_target_native_probe_category': 'succeeded',
        'final_target_native_resource_count': 2,
        'final_target_native_security_group_count': 1,
        'final_target_native_vswitch_count': 0,
        'StackId': 'private-stack', 'Resources': [{'PhysicalResourceId': 'private-id'}],
    }})
    assert public['diagnostics'] == {
        'final_target_native_resources_inspected': True,
        'final_target_native_probe_category': 'succeeded',
        'final_target_native_resource_count': 2,
        'final_target_native_security_group_count': 1,
        'final_target_native_vswitch_count': 0,
    }
    assert 'private' not in json.dumps(public)


def test_public_live_summary_smoke_diagnostics_do_not_export_task_or_response_content():
    public = run_e2e._public_live_summary({'diagnostics': {
        'smoke_sync_output_length': 100,
        'smoke_sync_task_count': 1,
        'smoke_sync_trailing_agent_message_count': 2,
        'smoke_sync_permission_hint': True,
        'smoke_sync_status_text_matches_stdout': True,
        'smoke_sync_history_vpc_marker': False,
        'smoke_sync_task_state': 'input-required',
        'smoke_sync_task_probe_category': 'verified',
        'smoke_sync_pending_kind': 'permission',
        'smoke_sync_pending_tool': 'write_file',
        'smoke_sync_pending_read_only': False,
        'smoke_sync_permission_response_count': 1,
        'task_id': 'private-task', 'stdout': 'private-secret', 'history': ['private-response'],
    }})
    assert len(public['diagnostics']) == 12
    assert 'private' not in json.dumps(public)
    bad = run_e2e._public_live_summary({'diagnostics': {
        'smoke_sync_output_length': -1,
        'smoke_sync_task_state': 'private-state',
        'smoke_sync_task_probe_category': 'private-error',
        'smoke_sync_history_vpc_marker': 'private-response',
        'smoke_sync_pending_kind': 'private-kind',
        'smoke_sync_pending_tool': 'private-tool',
        'smoke_sync_pending_read_only': 'private-value',
    }})
    assert 'diagnostics' not in bad


@pytest.mark.parametrize('field,value', [
    ('smoke_sync_permission_metadata_present', False),
    ('smoke_sync_permission_schema_supported', True),
    ('smoke_sync_permission_task_matches', False),
    ('smoke_sync_permission_context_matches', False),
    ('smoke_sync_permission_identity_complete', True),
    ('smoke_sync_permission_workspace_allowed', False),
    ('smoke_sync_permission_target_shape', 'multiple'),
    ('smoke_sync_permission_effect', 'file_change'),
])
def test_public_smoke_permission_diagnostics_accept_only_closed_values(field, value):
    public = run_e2e._public_live_summary({'diagnostics': {field: value}})
    assert public['diagnostics'] == {field: value}
    bad = run_e2e._public_live_summary({'diagnostics': {field: 'private-target-or-identity'}})
    assert 'diagnostics' not in bad
