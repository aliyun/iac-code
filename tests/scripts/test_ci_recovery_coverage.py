"""Fail-closed acceptance and suite routing for the recovery E2Es."""

import json
from itertools import product
from types import SimpleNamespace

import pytest

from scripts.a2a.e2e.common import StreamSummary
from scripts.a2a.e2e.permission_wait import run_namespace_handoff, run_permission_prompt_handoff
from scripts.a2a.e2e.resource_selector.live_pipeline_server import _followup_request_facts
from scripts.a2a.e2e.resource_selector.run_live_resource_selector import (
    _assert_next_turn_reply,
    _next_turn_evidence,
    _NextTurnReplyMismatchError,
)
from scripts.ci import run_e2e


def test_selector_restart_matrix_uses_readonly_isolated_live_adapter():
    args = run_e2e.parse_args(["--suite", "live-readonly", "--list"])
    cases = [
        case for case in run_e2e.select_cases(args) if case.script.endswith("run_live_resource_selector_restart.py")
    ]
    assert {case.args for case in cases} == {
        ("--mode", mode, "--restart-style", style, "--answer", answer)
        for mode, style, answer in product(
            ("normal", "pipeline"), ("sigterm", "sigkill", "released"), ("selected", "canceled")
        )
    }
    assert all(case.live_runner == "selector" and not case.cloud_write for case in cases)
    assert all(run_e2e._live_cleanup_status(case, None) == "not-needed" for case in cases)


@pytest.mark.parametrize("junit", ["<testsuites/>", "<testsuites><testcase><skipped/></testcase></testsuites>"])
def test_pytest_exit_zero_cannot_hide_missing_or_skipped_e2e(tmp_path, monkeypatch, junit):
    monkeypatch.setattr(
        "sys.argv",
        ["runner", "--run-dir", str(tmp_path / "run"), "--scenario", "artifact-edit-restart"],
    )
    monkeypatch.setattr(run_permission_prompt_handoff.os, "chdir", lambda _path: None)

    def fake_pytest(args):
        path = next(item.removeprefix("--junitxml=") for item in args if item.startswith("--junitxml="))
        from pathlib import Path

        Path(path).write_text(junit, encoding="utf-8")
        return 0

    monkeypatch.setattr(run_permission_prompt_handoff.pytest, "main", fake_pytest)
    assert run_permission_prompt_handoff.main() == 1


def test_namespace_capability_absence_is_failure(monkeypatch):
    monkeypatch.setattr(run_namespace_handoff.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Real Linux PID namespaces"):
        run_namespace_handoff._backend("auto")


def test_docker_client_without_server_is_not_a_namespace_backend(monkeypatch):
    monkeypatch.setattr(run_namespace_handoff.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        run_namespace_handoff.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"")
    )
    with pytest.raises(RuntimeError, match="Real Linux PID namespaces"):
        run_namespace_handoff._backend("docker")


def test_root_namespace_probe_can_work_when_user_namespace_is_disabled(monkeypatch):
    monkeypatch.setattr(run_namespace_handoff.platform, "system", lambda: "Linux")
    monkeypatch.setattr(run_namespace_handoff.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        run_namespace_handoff.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=int("--user" in command), stderr=b"disabled"),
    )
    assert run_namespace_handoff._backend("unshare") == "unshare-root"


def test_selector_restart_failure_reports_only_allowlisted_stage():
    safe = run_e2e._public_live_summary({"selector_restart_failure_stage": "answer-receipt", "error": "private"})
    assert safe["selector_restart_failure_stage"] == "answer-receipt"
    assert "private" not in str(safe)
    assert "selector_restart_failure_stage" not in run_e2e._public_live_summary(
        {
            "selector_restart_failure_stage": "private",
        }
    )


def test_restart_native_evidence_reads_only_its_config_root(tmp_path):
    config = tmp_path / "isolated-config"
    native = config / "projects/session.jsonl"
    native.parent.mkdir(parents=True)
    native.write_text(
        "\n".join(
            json.dumps(message)
            for message in [
                {"role": "user", "content": "TOKEN"},
                {"role": "assistant", "content": "TOKEN"},
            ]
        ),
        encoding="utf-8",
    )
    evidence = _next_turn_evidence(tmp_path / "scenario", config, "TOKEN", StreamSummary(name="next-turn", prompt=""))
    assert evidence["selector_next_turn_native_prompt_present"] is True
    assert evidence["selector_next_turn_native_token_present"] is True
    assert evidence["selector_next_turn_scan_complete"] is True


def test_restart_adapter_refreshes_and_diagnoses_the_actual_isolated_config(tmp_path, monkeypatch):
    script = tmp_path / "run_live_resource_selector_restart.py"
    script.write_text(
        "import argparse,json,time\nfrom pathlib import Path\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--run-dir',type=Path)\n"
        "a,_=p.parse_known_args()\n"
        "a.run_dir.mkdir(parents=True)\ntime.sleep(0.2)\n"
        "(a.run_dir/'summary.json').write_text(json.dumps({'passed':True}),encoding='utf-8')\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.yml").write_text("fake-key", encoding="utf-8")
    (source / "settings.yml").write_text("activeProvider: dashscope\n", encoding="utf-8")
    refreshed = []
    diagnosed = []
    monkeypatch.setattr(run_e2e, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_e2e, "CLOUD_REFRESH_SECONDS", 0.02)
    monkeypatch.setattr(
        run_e2e, "_prepare_cloud_credentials", lambda _helper, config, _python: refreshed.append(config)
    )
    monkeypatch.setattr(
        run_e2e,
        "collect_live_diagnostics",
        lambda *args, runtime_config_dir: diagnosed.append(runtime_config_dir) or {},
    )
    case = run_e2e.Case("restart", script.name, (), 5, "live", live_runner="selector", group="readonly")
    result = run_e2e.run_case(case, tmp_path / "report", source, cloud_credential_helper=tmp_path / "helper")
    expected = tmp_path / "report/runs/restart/config"
    assert result["status"] == "passed"
    assert len(refreshed) > 1 and all(path == expected for path in refreshed)
    assert diagnosed == [expected]


@pytest.mark.parametrize("latest_has_marker", [True, False])
def test_model_wire_probe_exports_only_facts_and_detects_message_order(latest_has_marker):
    marker = "RESTART_NEXT_TURN_OK_private"
    messages = [{"role": "system", "content": "private system"}, {"role": "user", "content": marker}]
    if not latest_has_marker:
        messages.append({"role": "user", "content": "private later user"})
    facts = _followup_request_facts({"messages": messages, "tool_choice": "auto"})
    assert facts["selector_next_turn_wire_last_user_has_marker"] is latest_has_marker
    assert facts["selector_next_turn_wire_last_message_has_marker"] is latest_has_marker
    assert facts["selector_next_turn_wire_step_prompt_present"] is False
    assert all(type(value) is bool for value in facts.values())
    assert "private" not in json.dumps(facts)
    assert _followup_request_facts({"messages": [{"role": "user", "content": "unrelated"}]}) is None


@pytest.mark.parametrize("standalone", [False, True])
def test_model_wire_probe_verifies_original_request_is_only_in_handoff(standalone):
    original = "private original request"
    summary = '[Pipeline Handoff Context]\nIncluded context:\n' + json.dumps({"original_user_request": original})
    messages = [{"role": "user", "content": summary}]
    if standalone:
        messages.insert(0, {"role": "user", "content": original})
    messages.append({"role": "user", "content": "RESTART_NEXT_TURN_OK_private"})
    facts = _followup_request_facts({"messages": messages})
    assert facts["selector_next_turn_wire_original_request_in_handoff"] is True
    assert facts["selector_next_turn_wire_original_request_standalone"] is standalone
    assert "private" not in json.dumps(facts)


@pytest.mark.parametrize("kind", ["receipt", "old-history", "new-pending"])
def test_next_turn_receipt_and_old_input_are_not_new_selector_waits(tmp_path, kind):
    payload = {"kind": "cloud_resource_selection", "inputId": "old"}
    field = "inputReceived" if kind == "receipt" else "inputRequired"
    if kind == "new-pending":
        payload["inputId"] = "new"
    metadata = {"iac_code": {field: payload}}
    record = {"result": {"metadata": metadata}}
    if kind == "old-history":
        record = {"result": {"task": {"history": [{"metadata": metadata}]}}}
    (tmp_path / "next-turn.events.jsonl").write_text(json.dumps(record), encoding="utf-8")
    summary = StreamSummary(
        name="next-turn", prompt="", text="TOKEN", context_id="ctx", status_states=["TASK_STATE_INPUT_REQUIRED"]
    )
    kwargs = dict(
        context_id="ctx", token="TOKEN", run_dir=tmp_path, config_dir=tmp_path / "config", previous_input_id="old"
    )
    if kind == "new-pending":
        with pytest.raises(_NextTurnReplyMismatchError):
            _assert_next_turn_reply(summary, **kwargs)
    else:
        assert all(_assert_next_turn_reply(summary, **kwargs).values())


def test_native_diagnostics_do_not_count_nested_history_as_new_tool_calls(tmp_path):
    native = tmp_path / "config/projects/session.jsonl"
    native.parent.mkdir(parents=True)
    rows = [
        {"role": "user", "content": "TOKEN"},
        {
            "role": "assistant",
            "content": "TOKEN",
            "metadata": {
                "history": [
                    {"role": "assistant", "content": [{"type": "tool_use", "name": "select_cloud_resource"}]},
                ]
            },
        },
    ]
    native.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    facts = _next_turn_evidence(tmp_path, tmp_path / "config", "TOKEN", StreamSummary(name="next-turn", prompt=""))
    assert facts["selector_next_turn_native_token_present"] is True
    assert facts["selector_next_turn_native_select_tool"] is False
