"""Offline checks for the live runner's evidence boundaries, not model answers."""

from types import SimpleNamespace

import pytest

from scripts.ci.a2a_context_evidence import fresh_history, main_requests, request_window, retained_history


@pytest.mark.parametrize("final_kind", ["permission", "ask_user_question", "candidate_selection", "completed"])
def test_permission_driver_rejects_exhaustion_but_keeps_business_waits(monkeypatch, tmp_path, final_kind):
    import json

    from scripts.ci import a2a_context_evidence as evidence

    def write_events(name, kind):
        payload = {"metadata": {"iac_code": {"input": {
            "kind": kind, "inputId": name, "toolName": "read_file", "toolUseId": name,
        }}}}
        (tmp_path / (name + ".events.jsonl")).write_text(json.dumps(payload) + "\n", encoding="utf-8")

    write_events("turn", "permission")
    calls = []

    def resume(**kwargs):
        calls.append(kwargs["prompt"])
        kind = final_kind if len(calls) == 8 else "permission"
        write_events(kwargs["name"], kind)
        state = "TASK_STATE_COMPLETED" if kind == "completed" else "TASK_STATE_INPUT_REQUIRED"
        return SimpleNamespace(context_id="ctx", task_id="task", last_status_state=state)

    monkeypatch.setattr(evidence, "tool_inputs", lambda _: [])
    monkeypatch.setattr(evidence, "_permission_response_query", lambda permission, decision: decision)
    monkeypatch.setattr(evidence, "stream_message", resume)
    response = SimpleNamespace(context_id="ctx", task_id="task", last_status_state="TASK_STATE_INPUT_REQUIRED")
    kwargs = dict(name="turn", run_dir=tmp_path, config=tmp_path, server_url="unused", cwd=str(tmp_path),
                  timeout=1, env={})
    if final_kind == "permission":
        with pytest.raises(RuntimeError, match="Permission requests remain unresolved after 8 responses"):
            evidence.finish_permissions(response, **kwargs)
    else:
        result = evidence.finish_permissions(response, **kwargs)
        assert result.last_status_state == ("TASK_STATE_COMPLETED" if final_kind == "completed"
                                           else "TASK_STATE_INPUT_REQUIRED")
    assert calls == ["deny"] * 8
    assert len(json.loads((tmp_path / "turn.permissions.json").read_text(encoding="utf-8"))) == 8


@pytest.mark.parametrize("variant,expected", [
    ("empty", True), ("wrapped_empty", True), ("status_only", True),
    ("failed_tool", False), ("stale_tool", False), ("changed_payload", False), ("missing_marker", False),
    ("missing_provider_evidence", False),
])
def test_step1_followup_checks_current_accepted_no_change_instead_of_raw_argument_shape(
    monkeypatch, tmp_path, variant, expected,
):
    from copy import deepcopy

    from scripts.ci import run_knowledge_wiki_a2a as wiki
    from scripts.ci import run_step_followup_a2a as runner

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT")
    original = {"status": "awaiting_selection", "candidate_set_id": "first", "candidates": [{"name": "ECS"}]}
    updated = {"status": "awaiting_selection", "candidate_set_id": "second", "candidates": [
        {"name": "ECS and RDS", "resource_inventory": [{"product": "RDS"}]},
    ]}
    conclusions = [original, original, original, updated, deepcopy(updated), {"status": "rejected"}]
    if variant == "changed_payload":
        conclusions[4]["candidates"] = [{"name": "different plan"}]
    calls, outcomes = [], {}

    class Server:
        def __init__(self, **kwargs):
            assert kwargs["env"]["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_ONLY"
            assert kwargs["env"]["IAC_CODE_TELEMETRY_LOCAL_ONLY"] == "1"
            self.stopped = False
            self.process = SimpleNamespace(poll=lambda: 0 if self.stopped else None)

        def start(self):
            pass

        def terminate(self):
            self.stopped = True

    class Capture:
        env = {}

        def __init__(self, *args):
            pass

        def start(self):
            return self

        def stop(self):
            return [{"attributes": {"gen_ai.request.model": "fixture"}}]

    def index(name):
        return int(str(name).split("-")[1].split(".")[0]) - 1

    def stream(**kwargs):
        i = index(kwargs["name"])
        payload = {"conclusion": {}}
        if i == 4:
            payload = {"conclusion": {"status": "awaiting_selection"}} if variant == "status_only" else payload
            if variant == "wrapped_empty":
                payload = {"conclusion": {"conclusion": {}}}
        if i != 4 or variant != "stale_tool":
            calls.append({"id": kwargs["name"], "name": "complete_step", "input": payload})
            outcomes[kwargs["name"]] = {"is_error": i == 4 and variant == "failed_tool"}
        return SimpleNamespace(
            context_id="fixture", task_id="fixture", response_content_type="text/event-stream",
            last_status_state="TASK_STATE_INPUT_REQUIRED", normal_handoff_ready=False,
        )

    def events(path):
        i = index(path.name)
        return [{"eventType": "step_completed", "data": {
            "conclusion": conclusions[i],
            "conclusion_unchanged": i in {1, 2, 4} and not (i == 4 and variant == "missing_marker"),
        }}]

    monkeypatch.setattr(runner, "ManagedServer", Server)
    monkeypatch.setattr(runner, "ObserveCapture", Capture)
    monkeypatch.setattr(wiki, "configure_permissions", lambda _: None)
    monkeypatch.setattr(runner, "_free_port", lambda _: 12345)
    monkeypatch.setattr(runner, "_write_server_config", lambda *a, **kw: tmp_path / "server.yml")
    monkeypatch.setattr(runner, "wait_for_server", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(runner, "finish_permissions", lambda response, **kw: response)
    monkeypatch.setattr(runner, "fetch_pipeline_state", lambda **kw: {})
    monkeypatch.setattr(runner, "tool_inputs", lambda _: list(calls))
    monkeypatch.setattr(runner, "results", lambda _: outcomes)
    monkeypatch.setattr(runner, "envelopes", events)
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _: "费用说明")
    monkeypatch.setattr(runner, "main_requests", lambda _: [{}])
    monkeypatch.setattr(runner, "request_window", lambda *a: [] if variant == "missing_provider_evidence" else [{}])
    monkeypatch.setattr(runner, "retained_history", lambda *a, **kw: {"retained": True})
    report = runner.run(SimpleNamespace(
        run_dir=tmp_path, provider="fixture", model="fixture", python="fixture", timeout=1, scenario="step1",
    ))
    assert report["status"] == ("passed" if expected else "failed")
    assert report["checks"]["changed candidates use new full batch"]
    assert report["checks"]["cancel ends step1"]
    failed = [key for key, ok in report["checks"].items() if not ok]
    if variant == "missing_provider_evidence":
        assert failed and all(key.endswith(": provider request evidence captured") for key in failed)
        assert not any(key.endswith(": first provider request keeps same-Step history") for key in report["checks"])
    else:
        assert all(key.startswith("turn 5:") for key in failed)


@pytest.mark.parametrize("ready,succeeded,has_quote", [
    (True, True, True),
    (False, False, True),
    (None, None, True),
    (False, False, False),
])
@pytest.mark.parametrize("captured", [True, False])
def test_context_runner_observes_preview_without_blocking_or_masking_missing_quote(
    monkeypatch, tmp_path, ready, succeeded, has_quote, captured,
):
    from scripts.ci import run_ecs_context_a2a as runner

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(config))
    selection = {"status": "awaiting_selection", "candidate_set_id": "batch", "candidates": [{"name": "ECS"}]}
    cost = {"quote_status": "succeeded", "preview_validation": {"succeeded": succeeded, "error": "private detail"}}
    materialized = {
        "status": "awaiting_confirmation", "preview_ready_for_create": ready,
        "selected_candidate_result": {"cost": cost if has_quote else {}},
    }
    conclusions = [selection, selection, materialized, {"status": "cancelled"}]
    calls = []

    class Server:
        instances = 0

        def __init__(self, **kwargs):
            assert kwargs["env"]["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_ONLY"
            assert kwargs["env"]["IAC_CODE_TELEMETRY_LOCAL_ONLY"] == "1"
            Server.instances += 1
            self.stopped = False
            self.process = SimpleNamespace(pid=Server.instances, poll=lambda: 0 if self.stopped else None)

        def start(self):
            pass

        def terminate(self):
            self.stopped = True

    class Capture:
        def __init__(self, *args):
            self.env = {}

        def start(self):
            return self

        def stop(self):
            return [{"attributes": {"gen_ai.request.model": "fixture"}}]

    def index(name):
        return int(str(name).split("-")[1].split(".")[0]) - 1

    def stream(**kwargs):
        calls.append({"id": kwargs["name"], "name": "complete_step", "input": {}})
        return SimpleNamespace(
            context_id="fixture", task_id="fixture", response_content_type="text/event-stream",
            last_status_state="TASK_STATE_COMPLETED" if index(kwargs["name"]) == 3 else "TASK_STATE_INPUT_REQUIRED",
        )

    def events(path):
        i = index(path.name)
        completed = {"eventType": "step_completed", "data": {
            "conclusion": conclusions[i], "conclusion_unchanged": i == 1,
        }}
        return ([{"eventType": "step_started", "step": {"id": "materialize_selected_candidate"}}]
                if i == 2 else []) + [completed]

    monkeypatch.setattr(runner, "ManagedServer", Server)
    monkeypatch.setattr(runner, "ObserveCapture", Capture)
    monkeypatch.setattr(runner, "configure_permissions", lambda _: None)
    monkeypatch.setattr(runner, "_free_port", lambda _: 12345)
    monkeypatch.setattr(runner, "_write_server_config", lambda *a, **kw: tmp_path / "server.yml")
    monkeypatch.setattr(runner, "wait_for_server", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "stream_message", stream)
    monkeypatch.setattr(runner, "finish_permissions", lambda response, **kw: response)
    monkeypatch.setattr(runner, "fetch_pipeline_state", lambda **kw: {})
    monkeypatch.setattr(runner, "tool_inputs", lambda _: list(calls))
    monkeypatch.setattr(runner, "results", lambda _: {call["id"]: {"is_error": False} for call in calls})
    monkeypatch.setattr(runner, "envelopes", events)
    monkeypatch.setattr(runner, "visible_pipeline_text", lambda _: "方案说明")
    monkeypatch.setattr(runner, "main_requests", lambda _: [{}])
    monkeypatch.setattr(runner, "request_window", lambda *a: [{}] if captured else [])
    monkeypatch.setattr(runner, "retained_history", lambda *a, **kw: {"retained": True})
    monkeypatch.setattr(runner, "fresh_history", lambda *a, **kw: True)
    report = runner.run(SimpleNamespace(
        run_dir=tmp_path, provider="fixture", model="fixture", python="fixture", timeout=1,
        scenario="selection-recovery",
    ))
    assert report["status"] == ("passed" if has_quote and captured else "failed")
    if not captured:
        assert report["checks"]["same Step1 history: provider request evidence captured"] is False
        assert "same Step1 history" not in report["checks"]
    assert report["checks"]["real materialization and quote"] is has_quote
    assert report["observations"]["materialization Preview"] == {
        "preview_ready_for_create": ready, "succeeded": succeeded if has_quote else None, "blocking": False,
    }
    assert report["checks"]["ends without deployment"]


def test_a2a_rollback_uses_the_public_wire_envelope():
    from scripts.ci.a2a_context_evidence import rolled_back_to

    assert rolled_back_to([{"eventType": "rollback_completed", "data": {"toStep": "step1"}}], "step1")
    assert rolled_back_to([{"eventType": "rollback_completed", "data": {"toStepId": "step1"}}], "step1")
    assert not rolled_back_to([{"eventType": "rollback_completed", "data": {"toStep": "other"}}], "step1")


def test_stale_name_only_is_invalid_but_current_index_is_authoritative():
    from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
    from iac_code.pipeline.engine.ui_contract import encode_selected_candidate

    runner = PipelineRunner.__new__(PipelineRunner)
    assert runner._infer_selected_index(encode_selected_candidate("old", 0), ["new"]) == 0
    assert runner._is_invalid_candidate_selection_payload(encode_selected_candidate("old", None), ["new"])


def test_history_checks_reject_missing_results_and_duplicated_user_turns():
    request = {"texts": ["original", "question"], "response_ids": {"read", "complete"}, "call_ids": {"read"}}
    assert all(retained_history(request, prompts=["original", "question"], response_ids={"read", "complete"}).values())
    assert not fresh_history(request, prompts=[], old_ids={"complete"})
    assert not fresh_history(request, prompts=["original"], old_ids=set())
    assert fresh_history(request, prompts=["previous-step"], old_ids={"previous-result"})
    assert not all(retained_history(request, prompts=["question"], response_ids={"missing"}).values())
    request["texts"].append("question")
    assert not all(retained_history(request, prompts=["question"], response_ids={"read"}).values())


def test_only_actual_main_provider_requests_can_prove_history():
    import json

    messages = [{"role": "user", "parts": [{"type": "text", "content": "question"}]}, {
        "role": "user", "parts": [{"type": "tool_call_response", "id": "read", "response": "[PATH]"}],
    }]
    attrs = {"gen_ai.input.messages": json.dumps(messages)}
    rows = [
        {"kind": "span", "name": "agent.run", "attributes": attrs, "timestamp_unix_nano": 1},
        {"kind": "span", "name": "chat model", "timestamp_unix_nano": 2,
         "attributes": {**attrs, "gen_ai.system_instructions": "Wiki keyword extraction"}},
        {"kind": "span", "name": "chat model", "attributes": attrs, "timestamp_unix_nano": 3},
    ]
    requests = main_requests(rows)
    assert len(requests) == 1
    assert requests[0]["response_ids"] == {"read"}
    assert request_window(requests, {"start_ns": 2, "end_ns": 4}) == requests
    assert request_window(requests, {"start_ns": 4, "end_ns": 5}) == []


@pytest.mark.parametrize("body", [None, "[]", "{}", "null", "not-json", '["not-a-message"]'])
def test_metadata_only_or_invalid_provider_spans_cannot_prove_empty_history(body):
    attrs = {} if body is None else {"gen_ai.input.messages": body}
    assert main_requests([{"kind": "span", "name": "chat model", "attributes": attrs}]) == []


@pytest.mark.parametrize("flag,enabled,expected", [(True, True, "allow_once"), (True, False, "deny"),
                                                (False, True, "deny"), (None, True, "deny")])
def test_dryrun_permission_is_opt_in_and_never_approves_missing_or_false_flag(
    monkeypatch, tmp_path, flag, enabled, expected,
):
    from scripts.ci import a2a_context_evidence as evidence

    permission = {"toolName": "aliyun_api", "toolUseId": "run", "isReadOnly": False}
    calls = [{"id": "run", "name": "aliyun_api", "input": {
        "product": "ecs", "action": "RunInstances", "params": {"DryRun": flag},
    }}]
    (tmp_path / "turn.events.jsonl").write_text("", encoding="utf-8")
    sent = []

    def resume(**kwargs):
        sent.append(kwargs["prompt"])
        (tmp_path / (kwargs["name"] + ".events.jsonl")).write_text("", encoding="utf-8")
        return SimpleNamespace(last_status_state="TASK_STATE_COMPLETED")

    monkeypatch.setattr(evidence, "tool_inputs", lambda _: calls)
    monkeypatch.setattr(evidence, "_unique_permissions", lambda _: [permission])
    monkeypatch.setattr(evidence, "_permission_response_query", lambda _, decision: decision)
    monkeypatch.setattr(evidence, "stream_message", resume)
    response = SimpleNamespace(last_status_state="TASK_STATE_INPUT_REQUIRED", context_id="ctx", task_id="task")
    evidence.finish_permissions(response, name="turn", run_dir=tmp_path, config=tmp_path, server_url="unused",
                                cwd=str(tmp_path), timeout=1, env={}, allow_ecs_dry_run=enabled)
    assert sent == [expected]
