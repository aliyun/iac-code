"""Offline contracts for ROS content-bearing display events, not lifecycle traffic."""

from __future__ import annotations

import pytest

from iac_code.a2a.display_timing import DisplayEventClassifier


def pipeline(kind, data=None, **fields):
    return {"eventType": kind, "data": data or {}, **fields}


def normal(iac):
    return {"statusUpdate": {"metadata": {"iac_code": iac}}}


def selector():
    return {
        "kind": "cloud_resource_selection",
        "inputId": "input-1",
        "toolUseId": "tool-1",
        "selector": {
            "id": "vpc",
            "associationProperty": "ALIYUN::ECS::VPC::VPCId",
            "outputKind": "resource",
        },
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        (pipeline("input_required", {"kind": "ask_user_question", "question": "用途是什么？"}), "question"),
        (
            pipeline("input_required", {"kind": "candidate_selection", "options": [{"name": "方案一"}]}),
            "candidate_selection",
        ),
        (
            pipeline("input_required", {"kind": "deployment_confirmation", "parameters": {"name": "demo"}}),
            "deployment_confirmation",
        ),
        (
            pipeline(
                "input_required",
                {
                    "kind": "deployment_confirmation",
                    "solution_summary": "测试网站",
                    "template_url": "templates/site.yml",
                    "cost": {"monthly_estimate": 100},
                },
            ),
            "deployment_confirmation",
        ),
        (pipeline("input_required", input=selector(), taskId="task-1", contextId="ctx-1"), "resource_selection"),
        (pipeline("permission_requested", permission={"inputId": "perm-1", "required": True}), "permission"),
        (pipeline("permission_requested", {"permissionId": "perm-1", "pending": True}), "permission"),
        (
            pipeline("candidate_detail_shown", {"detail": {"candidateName": "方案一", "summary": "小型网站"}}),
            "candidate_detail",
        ),
        (pipeline("diagram_shown", {"mermaidSource": "graph TD; A-->B"}), "diagram"),
        (pipeline("diagram_shown", {"templateContent": "{}"}), "diagram"),
        (pipeline("text_delta", {"text": "需求说明"}), "text"),
        (pipeline("thinking_delta", {"text": "考虑区域选择"}), "thinking"),
        (pipeline("tool_result", {"isError": True}), "tool_error"),
        (
            pipeline(
                "tool_result", {"toolName": "ros_stack", "result": '{"stack_id":"stack-1","progress_percentage":0}'}
            ),
            "deployment_result",
        ),
        (pipeline("candidate_completed", {"conclusions": {"template": {"template": "{}"}}}), "candidate_detail"),
        (
            pipeline(
                "candidate_step_completed",
                {"conclusion": {"cost": {"monthly_estimate": 100}}},
                candidateStep={"id": "cost_estimating"},
            ),
            "candidate_detail",
        ),
        (pipeline("stack_progress", {"stackId": "stack-1"}), "deployment_result"),
        (pipeline("step_completed", {"conclusion": {"summary": "方案已生成"}}), "step_result"),
        (normal({"input": {"kind": "permission", "inputId": "perm-1", "pending": True}}), "permission"),
        (normal({"thinking": {"type": "raw_thinking", "text": "思考内容"}}), "thinking"),
        (normal({"tool": {"status": "started", "name": "read_file", "toolUseId": "tool-1"}}), "tool"),
        (normal({"tool": {"status": "completed", "name": "read_file", "toolUseId": "tool-1"}}), "tool"),
        ({"message": {"messageId": "answer-1", "role": "ROLE_AGENT", "parts": [{"text": "答复"}]}}, "text"),
        ({"error": {"code": -32602, "message": "错误"}}, "error"),
    ],
)
def test_effective_display_content(value, expected):
    assert DisplayEventClassifier().classify({"result": value}) == expected


@pytest.mark.parametrize(
    "value",
    [
        {"task": {"status": {"message": {"role": "ROLE_AGENT", "parts": [{"text": "历史回复"}]}}}},
        {"kind": "task", "parts": [{"text": "历史回复"}]},
        {"id": "task-1", "status": {"state": "working"}},
        {"artifactUpdate": {"artifact": {"parts": [{"text": "工具文件内容"}]}}},
        pipeline("pipeline_started"),
        pipeline("step_started", {"name": "生成方案"}),
        pipeline("candidate_started", {"candidateName": "候选占位"}),
        pipeline("candidate_step_started"),
        pipeline("message_started"),
        pipeline("usage", {"outputTokens": 20}),
        pipeline("context_usage"),
        pipeline("input_received", {"prompt": "已接收"}),
        pipeline("permission_resolved", {"inputId": "perm-1", "decision": "allow_once"}),
        pipeline("tool_started", {"toolName": "ask_user_question", "toolUseId": "tool-1"}),
        pipeline("tool_result", {"toolName": "read_file", "result": "工具内部输出"}),
        pipeline("tool_result", {"toolName": "ros_stack", "result": '{"stack_id":"stack-1"}'}),
        pipeline("candidate_completed"),
        pipeline("candidate_step_completed"),
        pipeline("candidate_step_completed", {"conclusion": {"summary": "状态说明"}}),
        pipeline("step_completed", {"conclusion": {"summary": "   "}}),
        pipeline("step_completed", {"conclusion": {"message": "完成"}}),
        pipeline("tool_progress", {"message": "运行中"}),
        pipeline("step_completed", {"conclusion": {"status": "success"}}),
        pipeline("input_required"),
        pipeline("input_required", {"kind": "ask_user_question", "question": "  "}),
        pipeline("input_required", {"kind": "candidate_selection", "options": [{}]}),
        pipeline("input_required", {"kind": "deployment_confirmation"}),
        pipeline("input_required", input=selector()),
        pipeline("permission_requested", {"permissionId": "perm-1", "autoApproved": True}),
        pipeline("permission_requested", {"inputId": [], "required": True}),
        pipeline("candidate_detail_shown", {"detail": {}}),
        pipeline("diagram_shown", {"mermaidSource": "  "}),
        pipeline("text_delta", {"text": "  "}),
        pipeline("thinking_delta", {"text": "signature", "is_metadata_only": True}),
        normal({"tool": {"status": "started", "toolUseId": "tool-1"}}),
        normal({"tool": {"status": "started", "toolUseId": "tool-1", "name": "create_plan"}}),
        normal({"permission": {"autoApproved": True, "toolName": "read_file"}}),
        normal({"inputReceived": {"kind": "permission", "accepted": True}}),
        normal({"mcpWarning": {"message": "后台配置提示"}}),
        {
            "statusUpdate": {
                "status": {
                    "state": "TASK_STATE_CANCELED",
                    "message": {
                        "messageId": "task-1-message",
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "已取消"}],
                    },
                }
            }
        },
        {"message": {"role": "ROLE_AGENT", "parts": [{"text": "已接收"}, {"data": {"kind": "permission_ack"}}]}},
        {"message": {"role": "ROLE_USER", "parts": [{"text": "用户输入"}]}},
        None,
        [],
        {"kind": {}},
        pipeline({}),
        pipeline("input_required", {"kind": {}}),
    ],
)
def test_lifecycle_receipts_placeholders_and_non_rendered_events_do_not_count(value):
    assert DisplayEventClassifier().classify({"result": value}) is None


def test_batch_prefers_first_effective_content_and_honors_empty_batch_over_single_event():
    classifier = DisplayEventClassifier()
    card = pipeline("input_required", {"kind": "ask_user_question", "question": "用途？"})
    value = normal(
        {"pipelineBatch": {"events": [pipeline("step_started"), card, pipeline("text_delta", {"text": "正文"})]}}
    )
    assert classifier.classify(value) == "question"
    assert classifier.classify(normal({"pipelineBatch": {"events": []}, "pipeline": card})) is None


def test_internal_protocol_blocks_split_across_text_and_thinking_do_not_count_as_display():
    classifier = DisplayEventClassifier()
    for chunk in ("<|DS", "ML|invoke name='internal'>", "隐藏内容", "</|DSML|in", "voke>"):
        assert classifier.classify(pipeline("thinking_delta", {"text": chunk}, pipelineRunId="run-1")) is None
    assert classifier.classify(pipeline("text_delta", {"text": "真实正文"}, pipelineRunId="run-1")) == "text"


def test_protocol_filter_state_is_scoped_to_pipeline_candidate_and_request():
    classifier = DisplayEventClassifier()
    hidden = pipeline("thinking_delta", {"text": "<|DSML|invoke>hidden"}, candidate={"runId": "candidate-1"})
    assert classifier.classify(hidden) is None
    other = pipeline("text_delta", {"text": "另一个候选"}, candidate={"runId": "candidate-2"})
    assert classifier.classify(other) == "text"
    assert DisplayEventClassifier().classify(pipeline("text_delta", {"text": "新请求"})) == "text"


def test_hidden_protocol_state_is_bounded_and_does_not_store_protocol_bodies():
    classifier = DisplayEventClassifier()
    assert classifier.classify(pipeline("text_delta", {"text": "<|DSML|invoke>" + "hidden" * 50000})) is None
    state = next(iter(classifier._pipeline_text.values()))
    assert len(state.pending) < 512
    for i in range(600):
        classifier.classify(pipeline("text_delta", {"text": "<|DS"}, pipelineRunId=str(i)))
    assert len(classifier._pipeline_text) == 512


def test_many_internal_protocol_blocks_mark_observation_incomplete_instead_of_unbounded_scanning():
    classifier = DisplayEventClassifier()
    assert classifier.classify(pipeline("text_delta", {"text": "<|DSML|invoke></|DSML|invoke>" * 2000})) is None
    assert classifier.incomplete is True
