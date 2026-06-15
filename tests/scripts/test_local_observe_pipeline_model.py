from scripts.observability.local_observe.pipeline_model import build_pipeline_model


def test_pipeline_model_splits_step_attempts_and_attaches_agent_rounds():
    records = [
        {
            "id": "step1",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_step_1",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
        {
            "id": "step2",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_step_2",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 2,
            },
        },
        {
            "id": "round2",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round_2",
            "parent_span_id": "s_step_2",
            "attributes": {"gen_ai.react.round": 1},
        },
        {
            "id": "chat2",
            "kind": "span",
            "name": "chat qwen-max",
            "trace_id": "t1",
            "span_id": "s_chat_2",
            "parent_span_id": "s_round_2",
            "attributes": {"gen_ai.request.model": "qwen-max"},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    assert run["pipeline_name"] == "selling"
    assert run["session_id"] == "sess"
    assert [(step["step_id"], step["step_attempt"]) for step in run["steps"]] == [("template", 1), ("template", 2)]
    assert run["steps"][1]["agent_rounds"][0]["round"] == 1
    assert run["steps"][1]["agent_rounds"][0]["children"][0]["record_id"] == "chat2"


def test_pipeline_model_attaches_agent_rounds_to_sub_steps():
    records = [
        {
            "id": "substep",
            "kind": "span",
            "name": "iac.pipeline.sub_step",
            "trace_id": "t1",
            "span_id": "s_substep",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "candidate.refine",
                "step_attempt": 1,
            },
        },
        {
            "id": "round1",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round_1",
            "parent_span_id": "s_substep",
            "attributes": {"gen_ai.react.round": 1},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    assert run["steps"][0]["step_id"] == "candidate.refine"
    assert run["steps"][0]["agent_rounds"][0]["record_id"] == "round1"


def test_pipeline_model_attaches_agent_rounds_under_entry_span():
    records = [
        {
            "id": "step",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_step",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
        {
            "id": "entry",
            "kind": "span",
            "name": "enter_ai_application_system",
            "trace_id": "t1",
            "span_id": "s_entry",
            "parent_span_id": "s_step",
            "attributes": {},
        },
        {
            "id": "round1",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round",
            "parent_span_id": "s_entry",
            "attributes": {"gen_ai.react.round": 1},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    assert run["steps"][0]["agent_rounds"][0]["record_id"] == "round1"


def test_pipeline_model_attaches_span_associated_logs_to_step_evidence_not_run_evidence():
    records = [
        {
            "id": "step",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_step",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "deploying",
                "step_attempt": 1,
            },
        },
        {
            "id": "entry",
            "kind": "span",
            "name": "enter_ai_application_system",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_entry",
            "parent_span_id": "s_step",
            "attributes": {},
        },
        {
            "id": "tool_span",
            "kind": "span",
            "name": "execute_tool ros_stack",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_tool",
            "parent_span_id": "s_entry",
            "attributes": {"gen_ai.tool.name": "ros_stack"},
        },
        {
            "id": "tool_log",
            "kind": "log",
            "name": "iac.tool.use.succeeded",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_tool",
            "parent_span_id": "",
            "attributes": {"event.name": "iac.tool.use.succeeded", "tool_name": "ros_stack"},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    step = run["steps"][0]
    assert "tool_log" in step["record_ids"]
    assert [record["id"] for record in step["evidence_records"]] == ["step", "tool_log"]
    assert [record["id"] for record in run["evidence_records"]] == []


def test_pipeline_model_includes_agent_round_record_attributes_for_drilldown():
    records = [
        {
            "id": "step",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_step",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
        {
            "id": "round1",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round",
            "parent_span_id": "s_step",
            "attributes": {
                "gen_ai.react.round": 1,
                "gen_ai.input.messages": '[{"role":"user","content":"debug prompt"}]',
            },
        },
        {
            "id": "tool1",
            "kind": "span",
            "name": "execute_tool create_template",
            "trace_id": "t1",
            "span_id": "s_tool",
            "parent_span_id": "s_round",
            "attributes": {
                "gen_ai.tool.name": "create_template",
                "gen_ai.tool.call.arguments": '{"template_type":"ros"}',
                "gen_ai.tool.call.result": '{"ok":true}',
            },
        },
    ]

    model = build_pipeline_model(records)

    round_record = model["runs"][0]["steps"][0]["agent_rounds"][0]
    assert round_record["attributes"]["gen_ai.input.messages"] == '[{"role":"user","content":"debug prompt"}]'
    assert round_record["children"][0]["attributes"]["gen_ai.tool.call.arguments"] == '{"template_type":"ros"}'
    assert round_record["children"][0]["attributes"]["gen_ai.tool.call.result"] == '{"ok":true}'


def test_pipeline_model_infers_metric_session_when_unique_step_run_exists():
    records = [
        {
            "id": "step_span",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_step",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
        {
            "id": "step_metric",
            "kind": "metric",
            "name": "iac.pipeline.step.duration",
            "attributes": {
                "pipeline_name": "selling",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
    ]

    model = build_pipeline_model(records)

    assert len(model["runs"]) == 1
    run = model["runs"][0]
    assert run["session_id"] == "sess"
    assert run["steps"][0]["record_ids"] == ["step_span", "step_metric"]


def test_pipeline_model_uses_resource_session_for_pipeline_metrics():
    records = [
        {
            "id": "step_span",
            "kind": "span",
            "name": "iac.pipeline.step",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_step",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
        {
            "id": "step_metric",
            "kind": "metric",
            "name": "iac.pipeline.step.duration",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 2,
            "value": 100.0,
            "attributes": {
                "pipeline_name": "selling",
                "step_id": "template",
                "step_attempt": 1,
                "status": "completed",
            },
        },
    ]

    model = build_pipeline_model(records)

    assert [(run["pipeline_name"], run["session_id"]) for run in model["runs"]] == [("selling", "sess")]
    assert model["unscoped_metrics"] == []
    assert model["runs"][0]["steps"][0]["record_ids"] == ["step_span", "step_metric"]


def test_pipeline_model_uses_resource_session_for_metric_only_pipeline_records():
    records = [
        {
            "id": "completion_metric",
            "kind": "metric",
            "name": "iac.pipeline.completion.time",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 2,
            "value": 1200.0,
            "attributes": {
                "pipeline_name": "selling",
                "status": "completed",
            },
        },
    ]

    model = build_pipeline_model(records)

    assert [(run["pipeline_name"], run["session_id"]) for run in model["runs"]] == [("selling", "sess")]
    assert model["unscoped_metrics"] == []
    assert model["runs"][0]["record_ids"] == ["completion_metric"]


def test_pipeline_model_does_not_create_unknown_runs_from_resource_only_agent_records():
    records = [
        {
            "id": "agent_round",
            "kind": "span",
            "name": "react step",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_round",
            "parent_span_id": "",
            "attributes": {"gen_ai.react.round": 1},
        },
        {
            "id": "run_metric",
            "kind": "metric",
            "name": "iac.pipeline.completion.time",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 2,
            "value": 1200.0,
            "attributes": {"pipeline_name": "selling", "status": "completed"},
        },
    ]

    model = build_pipeline_model(records)

    assert [(run["pipeline_name"], run["session_id"]) for run in model["runs"]] == [("selling", "sess")]
    assert model["runs"][0]["record_ids"] == ["run_metric", "agent_round"]
    assert [record["id"] for record in model["runs"][0]["evidence_records"]] == ["run_metric", "agent_round"]


def test_pipeline_model_attaches_normal_chat_records_to_existing_session_run():
    records = [
        {
            "id": "run_metric",
            "kind": "metric",
            "name": "iac.pipeline.completion.time",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 2,
            "value": 1200.0,
            "attributes": {"pipeline_name": "selling", "status": "user_aborted"},
        },
        {
            "id": "normal_entry",
            "kind": "span",
            "name": "enter_ai_application_system",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t2",
            "span_id": "s_normal_entry",
            "parent_span_id": "",
            "attributes": {"gen_ai.input.messages": '[{"role":"user","content":"after pipeline"}]'},
        },
        {
            "id": "normal_round",
            "kind": "span",
            "name": "react step",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t2",
            "span_id": "s_normal_round",
            "parent_span_id": "s_normal_entry",
            "attributes": {"gen_ai.react.round": 1},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    assert run["record_ids"] == ["run_metric", "normal_entry", "normal_round"]
    assert [record["id"] for record in run["evidence_records"]] == ["run_metric", "normal_entry", "normal_round"]
    groups = {group["id"]: group for group in run["evidence_groups"]}
    assert [record["id"] for record in groups["pipeline_lifecycle"]["records"]] == ["run_metric"]
    assert [record["id"] for record in groups["normal_chat_after_pipeline"]["records"]] == [
        "normal_entry",
        "normal_round",
    ]


def test_pipeline_model_splits_run_evidence_into_lifecycle_normal_chat_and_other():
    records = [
        {
            "id": "pipeline_started",
            "kind": "log",
            "name": "iac.pipeline.started",
            "resource": {"session.id": "iac_sess_sess"},
            "attributes": {"pipeline_name": "selling", "session_id": "sess"},
        },
        {
            "id": "pipeline_run",
            "kind": "span",
            "name": "iac.pipeline.run",
            "resource": {"session.id": "iac_sess_sess"},
            "span_id": "s_pipeline_run",
            "attributes": {"pipeline_name": "selling", "session_id": "sess"},
        },
        {
            "id": "normal_entry",
            "kind": "span",
            "name": "enter_ai_application_system",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "normal_trace",
            "span_id": "s_normal_entry",
            "attributes": {"gen_ai.input.messages": '[{"role":"user","content":"normal"}]'},
        },
        {
            "id": "normal_api_log",
            "kind": "log",
            "name": "iac.api.request.started",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "normal_trace",
            "span_id": "s_chat",
            "attributes": {"event.name": "iac.api.request.started"},
        },
        {
            "id": "session_started",
            "kind": "log",
            "name": "iac.session.started",
            "resource": {"session.id": "iac_sess_sess"},
            "attributes": {"event.name": "iac.session.started"},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    groups = {group["id"]: group for group in run["evidence_groups"]}
    assert [record["id"] for record in groups["pipeline_lifecycle"]["records"]] == [
        "pipeline_started",
        "pipeline_run",
    ]
    assert [record["id"] for record in groups["normal_chat_after_pipeline"]["records"]] == [
        "normal_entry",
        "normal_api_log",
    ]
    assert [record["id"] for record in groups["other_session_evidence"]["records"]] == ["session_started"]


def test_pipeline_model_attaches_parent_step_metrics_using_resource_session():
    records = [
        {
            "id": "parent_step",
            "kind": "span",
            "name": "iac.pipeline.step",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_parent",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "evaluate_candidates",
                "step_attempt": 1,
            },
        },
        {
            "id": "sub_pipeline_metric",
            "kind": "metric",
            "name": "iac.pipeline.sub_pipeline.duration",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 2,
            "value": 197638.732,
            "attributes": {
                "pipeline_name": "selling",
                "parent_step_id": "evaluate_candidates",
                "status": "completed",
            },
        },
    ]

    model = build_pipeline_model(records)

    assert model["unscoped_metrics"] == []
    assert model["runs"][0]["steps"][0]["record_ids"] == ["parent_step", "sub_pipeline_metric"]


def test_pipeline_model_compacts_repeated_cumulative_metrics_to_latest_record():
    metric_attrs = {
        "pipeline_name": "selling",
        "step_id": "template",
        "step_attempt": 1,
        "status": "completed",
    }
    records = [
        {
            "id": "step_span",
            "kind": "span",
            "name": "iac.pipeline.step",
            "resource": {"session.id": "iac_sess_sess"},
            "trace_id": "t1",
            "span_id": "s_step",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "template",
                "step_attempt": 1,
            },
        },
        {
            "id": "metric_old",
            "kind": "metric",
            "name": "iac.pipeline.step.duration",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 1,
            "value": 100.0,
            "attributes": metric_attrs,
            "aggregation_temporality": 2,
        },
        {
            "id": "metric_latest",
            "kind": "metric",
            "name": "iac.pipeline.step.duration",
            "resource": {"session.id": "iac_sess_sess"},
            "timestamp_unix_nano": 2,
            "value": 100.0,
            "attributes": metric_attrs,
            "aggregation_temporality": 2,
        },
    ]

    model = build_pipeline_model(records)

    assert model["runs"][0]["steps"][0]["record_ids"] == ["step_span", "metric_latest"]


def test_pipeline_model_keeps_unscoped_metrics_out_of_unknown_runs():
    records = [
        {
            "id": "metric_only",
            "kind": "metric",
            "name": "iac.pipeline.funnel.step.count",
            "value": 1,
            "attributes": {
                "pipeline_name": "selling",
                "step_id": "confirm_and_select",
                "step_attempt": 1,
                "status": "waiting_input",
            },
        },
        {
            "id": "run_span",
            "kind": "span",
            "name": "iac.pipeline.run",
            "trace_id": "t1",
            "span_id": "s_run",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
            },
        },
    ]

    model = build_pipeline_model(records)

    assert [(run["pipeline_name"], run["session_id"]) for run in model["runs"]] == [("selling", "sess")]
    assert model["unscoped_metrics"] == [
        {
            "record_id": "metric_only",
            "name": "iac.pipeline.funnel.step.count",
            "value": 1,
            "attributes": records[0]["attributes"],
        }
    ]


def test_pipeline_model_uses_real_sub_step_id_and_nearest_pipeline_span_for_rounds():
    records = [
        {
            "id": "parent_step",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_parent",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "evaluate_candidates",
                "step_attempt": 1,
            },
        },
        {
            "id": "substep",
            "kind": "span",
            "name": "iac.pipeline.sub_step",
            "trace_id": "t1",
            "span_id": "s_substep",
            "parent_span_id": "s_parent",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "parent_step_id": "evaluate_candidates",
                "sub_pipeline_id": "evaluate_candidate_a",
                "sub_step_id": "template_generating",
                "step_attempt": 1,
            },
        },
        {
            "id": "entry",
            "kind": "span",
            "name": "enter_ai_application_system",
            "trace_id": "t1",
            "span_id": "s_entry",
            "parent_span_id": "s_substep",
            "attributes": {},
        },
        {
            "id": "round1",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round",
            "parent_span_id": "s_entry",
            "attributes": {"gen_ai.react.round": 1},
        },
    ]

    model = build_pipeline_model(records)

    run = model["runs"][0]
    parent_step = next(step for step in run["steps"] if step["step_id"] == "evaluate_candidates")
    sub_step = next(step for step in run["steps"] if step["step_id"] == "template_generating")
    assert parent_step["agent_rounds"] == []
    assert sub_step["record_ids"] == ["substep"]
    assert sub_step["agent_rounds"][0]["record_id"] == "round1"


def test_pipeline_model_keeps_parallel_sub_steps_with_same_name_separate():
    records = [
        {
            "id": "parent_step",
            "kind": "span",
            "name": "iac.pipeline.step",
            "trace_id": "t1",
            "span_id": "s_parent",
            "parent_span_id": "",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "step_id": "evaluate_candidates",
                "step_attempt": 1,
            },
        },
        {
            "id": "substep_a",
            "kind": "span",
            "name": "iac.pipeline.sub_step",
            "trace_id": "t1",
            "span_id": "s_substep_a",
            "parent_span_id": "s_parent",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "parent_step_id": "evaluate_candidates",
                "sub_pipeline_id": "evaluate_candidate_a",
                "sub_step_id": "template_generating",
                "candidate_index": 0,
            },
        },
        {
            "id": "entry_a",
            "kind": "span",
            "name": "enter_ai_application_system",
            "trace_id": "t1",
            "span_id": "s_entry_a",
            "parent_span_id": "s_substep_a",
            "attributes": {},
        },
        {
            "id": "round_a",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round_a",
            "parent_span_id": "s_entry_a",
            "attributes": {"gen_ai.react.round": 1},
        },
        {
            "id": "metric_a",
            "kind": "metric",
            "name": "iac.pipeline.sub_step.duration",
            "attributes": {
                "pipeline_name": "selling",
                "parent_step_id": "evaluate_candidates",
                "sub_step_id": "template_generating",
                "candidate_index": 0,
            },
        },
        {
            "id": "substep_b",
            "kind": "span",
            "name": "iac.pipeline.sub_step",
            "trace_id": "t1",
            "span_id": "s_substep_b",
            "parent_span_id": "s_parent",
            "attributes": {
                "pipeline_name": "selling",
                "session_id": "sess",
                "parent_step_id": "evaluate_candidates",
                "sub_pipeline_id": "evaluate_candidate_b",
                "sub_step_id": "template_generating",
                "candidate_index": 1,
            },
        },
        {
            "id": "entry_b",
            "kind": "span",
            "name": "enter_ai_application_system",
            "trace_id": "t1",
            "span_id": "s_entry_b",
            "parent_span_id": "s_substep_b",
            "attributes": {},
        },
        {
            "id": "round_b",
            "kind": "span",
            "name": "react step",
            "trace_id": "t1",
            "span_id": "s_round_b",
            "parent_span_id": "s_entry_b",
            "attributes": {"gen_ai.react.round": 1},
        },
        {
            "id": "metric_b",
            "kind": "metric",
            "name": "iac.pipeline.sub_step.duration",
            "attributes": {
                "pipeline_name": "selling",
                "parent_step_id": "evaluate_candidates",
                "sub_step_id": "template_generating",
                "candidate_index": 1,
            },
        },
    ]

    model = build_pipeline_model(records)

    assert {run["session_id"] for run in model["runs"]} == {"sess"}
    run = model["runs"][0]
    parent_step = next(step for step in run["steps"] if step["step_id"] == "evaluate_candidates")
    sub_steps = sorted(
        [step for step in run["steps"] if step["step_id"] == "template_generating"],
        key=lambda step: step["sub_pipeline_id"],
    )
    assert parent_step["agent_rounds"] == []
    assert [step["step_instance_id"] for step in sub_steps] == [
        "evaluate_candidate_a/template_generating",
        "evaluate_candidate_b/template_generating",
    ]
    assert [{*step["record_ids"]} for step in sub_steps] == [
        {"substep_a", "metric_a"},
        {"substep_b", "metric_b"},
    ]
    assert [step["agent_rounds"][0]["record_id"] for step in sub_steps] == ["round_a", "round_b"]
