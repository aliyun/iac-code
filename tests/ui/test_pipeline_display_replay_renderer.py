import json

from rich.console import Console
from rich.text import Text

from iac_code.agent.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from iac_code.pipeline.engine.display_replay import (
    DisplayAttempt,
    DisplayCandidate,
    DisplayCandidateSelection,
    DisplayReplayModel,
    DisplaySubPipeline,
    DisplaySubStepAttempt,
    DisplayToolUse,
)
from iac_code.ui.pipeline_display_replay import PipelineDisplayReplayRenderer
from iac_code.ui.pipeline_styles import PIPELINE_STEP_HEADER_STYLE, PIPELINE_TITLE_STYLE


class _CaptureConsole:
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        if args:
            self.printed.extend(args)
        else:
            self.printed.append(None)


def _render_text(model: DisplayReplayModel) -> str:
    console = Console(record=True, width=100, height=60)
    PipelineDisplayReplayRenderer(console).render(model)
    return console.export_text()


def test_renderer_uses_slate_sky_pipeline_label_styles():
    console = _CaptureConsole()
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="intent_parsing",
                attempt_no=1,
                index=1,
                total=5,
            )
        ],
    )

    PipelineDisplayReplayRenderer(console).render(model)

    text_items = [item for item in console.printed if isinstance(item, Text)]
    title = next(item for item in text_items if item.plain == " AI Selling Pipeline ")
    step = next(item for item in text_items if item.plain == "● Intent parsing (1/5) ")
    assert title.spans[0].style == PIPELINE_TITLE_STYLE
    assert step.spans[0].style == PIPELINE_STEP_HEADER_STYLE


def test_renderer_prints_generic_pipeline_history():
    model = DisplayReplayModel(
        pipeline_name="selling",
        interrupted=True,
        attempts=[
            DisplayAttempt(
                step_id="intent_parsing",
                attempt_no=1,
                index=1,
                total=5,
                status="completed",
                tools=[DisplayToolUse(name="complete_step", tool_use_id="tu_1")],
            ),
            DisplayAttempt(
                step_id="evaluate_candidates",
                attempt_no=1,
                index=3,
                total=5,
                status="interrupted",
                step_type="parallel_sub_pipeline",
                sub_pipelines={
                    "candidate_0": DisplaySubPipeline(
                        sub_pipeline_id="candidate_0",
                        candidate_index=0,
                        candidate_name="低成本方案",
                        status="running",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=1,
                                status="completed",
                            ),
                            DisplaySubStepAttempt(
                                step_id="cost_estimating",
                                attempt_no=1,
                                status="running",
                            ),
                        ],
                    )
                },
            ),
        ],
    )

    text = _render_text(model)

    assert "AI Selling Pipeline" in text
    assert "Intent parsing" in text
    assert "Complete step" in text
    assert "Evaluate candidates" in text
    assert "低成本方案" in text
    assert "[1/2]" in text
    assert "Cost estimation" in text
    assert "intent_parsing" not in text
    assert "complete_step" not in text
    assert "evaluate_candidates" not in text
    assert "cost_estimating" not in text
    assert "Waiting for output" not in text
    assert "   - 低成本方案" not in text
    assert "Interrupted" in text


def test_renderer_prints_candidate_selection_waiting_state_with_candidate_selection_ui():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="confirm_and_select",
                attempt_no=1,
                index=4,
                total=5,
                status="waiting_input",
                ui_mode="candidate_selection",
                candidate_selection=DisplayCandidateSelection(
                    state="waiting",
                    prompt="请选择一个方案",
                    options=[{"name": "低成本方案", "summary": "单 ECS", "candidate_index": 0}],
                    candidates={
                        0: DisplayCandidate(
                            name="低成本方案",
                            candidate_index=0,
                            mermaid_source="graph TD\n  A[公网] --> B[Nginx]",
                            summary="单 ECS",
                            cost_items=[{"name": "ECS", "spec": "1C1G", "monthly_cost": "¥30/月"}],
                            total_monthly_cost="¥30/月",
                        )
                    },
                ),
            )
        ],
    )

    text = _render_text(model)

    assert "Confirm and select" in text
    assert "低成本方案" in text
    assert "Cost details" in text
    assert "¥30/月" in text
    assert "Press number keys to select a candidate" in text
    assert "等待用户选择" not in text
    assert "请选择一个方案" not in text


def test_interrupted_candidate_selection_preparing_state_uses_candidate_selection_ui():
    model = DisplayReplayModel(
        pipeline_name="selling",
        interrupted=True,
        attempts=[
            DisplayAttempt(
                step_id="confirm_and_select",
                attempt_no=1,
                index=4,
                total=5,
                status="interrupted",
                ui_mode="candidate_selection",
                candidate_selection=DisplayCandidateSelection(
                    state="preparing",
                    candidates={
                        0: DisplayCandidate(
                            name="在已有 VPC 中新建 VSwitch",
                            candidate_index=0,
                            mermaid_source="graph TD\n  A[已有 VPC] --> B[VSwitch]",
                            summary="在已有 VPC 中创建一个新的 VSwitch。",
                            cost_items=[{"name": "VSwitch", "spec": "192.168.20.0/24", "monthly_cost": "¥0/月"}],
                            total_monthly_cost="¥0/月",
                        )
                    },
                ),
            )
        ],
    )

    text = _render_text(model)

    assert "Confirm and select" in text
    assert "在已有 VPC 中新建 VSwitch" in text
    assert "在已有 VPC 中创建一个新的 VSwitch。" in text
    assert "Cost details" in text
    assert "¥0/月" in text
    assert "↑ ↓ scroll" in text
    assert "候选方案展示准备中" not in text
    assert "   - 在已有 VPC 中新建 VSwitch #1" not in text
    assert "Interrupted" in text


def test_renderer_prints_candidate_selection_selected_state():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="confirm_and_select",
                attempt_no=1,
                status="running",
                ui_mode="candidate_selection",
                candidate_selection=DisplayCandidateSelection(
                    state="selected",
                    selected_name="低成本方案",
                    selected_index=0,
                    candidates={0: DisplayCandidate(name="低成本方案", candidate_index=0, summary="单 ECS")},
                ),
            )
        ],
    )

    text = _render_text(model)

    assert "Selected" in text
    assert "低成本方案" in text
    assert "单 ECS" in text


def test_renderer_prints_candidate_selection_completed_selected_state():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="confirm_and_select",
                attempt_no=1,
                status="completed",
                ui_mode="candidate_selection",
                candidate_selection=DisplayCandidateSelection(
                    state="completed",
                    selected_name="低成本方案",
                    selected_index=0,
                    candidates={0: DisplayCandidate(name="低成本方案", candidate_index=0, summary="单 ECS")},
                ),
            )
        ],
    )

    text = _render_text(model)

    assert "Selected" in text
    assert "低成本方案" in text
    assert "单 ECS" in text


def test_completed_parallel_sub_pipeline_renders_candidate_summary_without_sub_step_transcripts():
    output: list[str] = []
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="evaluate_candidates",
                attempt_no=1,
                index=3,
                total=5,
                status="completed",
                step_type="parallel_sub_pipeline",
                sub_pipelines={
                    "candidate_0": DisplaySubPipeline(
                        sub_pipeline_id="candidate_0",
                        candidate_name="轻量应用服务器一键部署",
                        status="completed",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=1,
                                status="completed",
                                transcript_id="transcript_att_1",
                            )
                        ],
                    ),
                    "candidate_1": DisplaySubPipeline(
                        sub_pipeline_id="candidate_1",
                        candidate_name="ECS 灵活部署",
                        status="completed",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="cost_estimating",
                                attempt_no=1,
                                status="completed",
                                transcript_id="transcript_att_2",
                            )
                        ],
                    ),
                },
            )
        ],
    )

    def replay_history(messages: list[Message]) -> None:
        output.extend(message.get_text() for message in messages)

    console = Console(record=True, width=100)
    PipelineDisplayReplayRenderer(
        console,
        history_replayer=replay_history,
        transcript_loader=lambda _transcript_id: [Message(role="assistant", content="SUBSTEP DETAILS")],
    ).render(model)

    text = console.export_text()
    assert "✓ 轻量应用服务器一键部署: Completed" in text
    assert "✓ ECS 灵活部署: Completed" in text
    assert "template_generating" not in text
    assert "SUBSTEP DETAILS" not in output


def test_interrupted_parallel_sub_pipeline_renders_tab_snapshot_without_expanding_all_transcripts():
    output: list[str] = []
    model = DisplayReplayModel(
        pipeline_name="selling",
        interrupted=True,
        attempts=[
            DisplayAttempt(
                step_id="evaluate_candidates",
                attempt_no=1,
                index=3,
                total=5,
                status="interrupted",
                step_type="parallel_sub_pipeline",
                sub_pipelines={
                    "candidate_0": DisplaySubPipeline(
                        sub_pipeline_id="candidate_0",
                        candidate_index=0,
                        candidate_name="经济型 Nginx 演示站",
                        total_steps=2,
                        status="running",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=1,
                                status="completed",
                                transcript_id="transcript_att_1",
                            ),
                            DisplaySubStepAttempt(
                                step_id="cost_estimating",
                                attempt_no=1,
                                status="interrupted",
                                transcript_id="transcript_att_2",
                            ),
                        ],
                    ),
                    "candidate_1": DisplaySubPipeline(
                        sub_pipeline_id="candidate_1",
                        candidate_index=1,
                        candidate_name="性能型 Nginx 演示站",
                        total_steps=2,
                        status="running",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=1,
                                status="running",
                                transcript_id="transcript_att_3",
                            )
                        ],
                    ),
                },
            )
        ],
    )

    def replay_history(messages: list[Message]) -> None:
        output.extend(message.get_text() for message in messages)

    loaded_transcripts: list[str] = []

    def load_transcript(transcript_id: str) -> list[Message]:
        loaded_transcripts.append(transcript_id)
        return {
            "transcript_att_1": [
                Message(role="user", content="请完成当前步骤：template_generating。"),
                Message(role="assistant", content=[TextBlock(text="已生成模板。")]),
            ],
            "transcript_att_2": [
                Message(role="user", content="请完成当前步骤：cost_estimating。"),
                Message(role="assistant", content=[TextBlock(text="费用估算中。")]),
            ],
            "transcript_att_3": [
                Message(role="assistant", content=[TextBlock(text="另一个候选的内容。")]),
            ],
        }.get(transcript_id, [])

    def render_history(messages: list[Message]):
        output.extend(message.get_text() for message in messages)
        return Text("\n".join(message.get_text() for message in messages))

    console = Console(record=True, width=100)
    PipelineDisplayReplayRenderer(
        console,
        history_replayer=replay_history,
        transcript_loader=load_transcript,
        history_renderable_factory=render_history,
    ).render(model)

    text = console.export_text()
    assert "经济型 Nginx 演示站" in text
    assert "性能型 Nginx 演示站" in text
    assert "← → switch candidates" in text
    assert "1-2 jump directly" in text
    assert "已生成模板。" in text
    assert "费用估算中。" in text
    assert "另一个候选的内容。" not in text
    assert loaded_transcripts == ["transcript_att_1", "transcript_att_2"]
    assert "Waiting for output" not in text
    assert "━━ 经济型 Nginx 演示站: template_generating ━━" not in text
    assert "   - 经济型 Nginx 演示站" not in text
    assert "请完成当前步骤" not in "".join(output)


def test_completed_candidate_selection_renders_only_selected_static_content():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="confirm_and_select",
                attempt_no=1,
                index=4,
                total=5,
                status="completed",
                ui_mode="candidate_selection",
                tools=[DisplayToolUse(name="complete_step", tool_use_id="tu_1")],
                candidate_selection=DisplayCandidateSelection(
                    state="completed",
                    selected_name="轻量应用服务器一键部署",
                    selected_index=0,
                    candidates={
                        0: DisplayCandidate(
                            name="轻量应用服务器一键部署",
                            candidate_index=0,
                            mermaid_source="graph TD\n  A[云服务器实例]",
                            summary="单台轻量应用服务器预装 Nginx 应用镜像。",
                            cost_items=[
                                {
                                    "name": "轻量应用服务器实例",
                                    "spec": "2C2G, 3Mbps 带宽",
                                    "monthly_cost": "¥40/月",
                                }
                            ],
                            total_monthly_cost="¥40/月",
                        ),
                        1: DisplayCandidate(
                            name="ECS 灵活部署",
                            candidate_index=1,
                            summary="在 VPC 内创建 ECS 实例安装 Nginx。",
                            total_monthly_cost="¥454/月",
                        ),
                    },
                ),
            )
        ],
    )

    text = _render_text(model)

    assert "✓ Selected: 轻量应用服务器一键部署" in text
    assert "单台轻量应用服务器预装 Nginx 应用镜像。" in text
    assert "Cost details" in text
    assert "ECS 灵活部署" not in text
    assert "complete_step" not in text
    assert "✓ Completed" not in text


def test_completed_pipeline_renders_terminal_footer():
    model = DisplayReplayModel(
        pipeline_name="selling",
        completed=True,
        attempts=[
            DisplayAttempt(
                step_id="deploying",
                attempt_no=1,
                index=5,
                total=5,
                status="completed",
            )
        ],
    )

    text = _render_text(model)

    assert "Pipeline completed" in text
    assert "Pipeline completed. Normal chat is now active." in text


def test_renderer_renders_ask_user_question_prompt_and_answer_from_transcript():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="intent_parsing",
                attempt_no=1,
                index=1,
                total=5,
                status="completed",
                transcript_id="transcript_att_1",
            )
        ],
    )
    transcript = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="ask_1",
                    name="ask_user_question",
                    input={
                        "question": "你的个人 Nginx 演示网站具体是什么形态？这会影响方案选型。",
                        "options": [
                            {"id": "static_site", "label": "静态网站（纯 HTML/CSS/JS 页面）"},
                            {"id": "reverse_proxy", "label": "反向代理（代理到后端服务）"},
                        ],
                        "allow_free_text": True,
                        "free_text_prompt": "或补充更多信息：预期访问量、是否需要公网访问/域名、预算等",
                    },
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="ask_1",
                    content=json.dumps(
                        {
                            "selected_id": "static_site",
                            "selected_label": "静态网站（纯 HTML/CSS/JS 页面）",
                            "free_text": "",
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        ),
    ]
    console = Console(record=True, width=100)
    PipelineDisplayReplayRenderer(
        console,
        history_replayer=lambda messages: None,
        transcript_loader=lambda _transcript_id: transcript,
    ).render(model)

    text = console.export_text()
    assert "你的个人 Nginx 演示网站具体是什么形态？" in text
    assert "1. 静态网站（纯 HTML/CSS/JS 页面）" in text
    assert "2. 反向代理（代理到后端服务）" in text
    assert "或补充更多信息" in text
    assert "> 静态网站（纯 HTML/CSS/JS 页面）" in text
    assert "selected_id" not in text


def test_renderer_does_not_treat_failed_ask_user_question_as_user_answer():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="intent_parsing",
                attempt_no=1,
                index=1,
                total=5,
                status="completed",
                transcript_id="transcript_att_1",
            )
        ],
    )
    invalid_error = "Invalid input for tool 'ask_user_question': options is not of type 'array'."
    valid_question = "个人 Nginx 演示网站 — 为了给你出 2 个合适的方案，需要补充几个关键信息："
    transcript = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="ask_failed",
                    name="ask_user_question",
                    input={"question": valid_question, "options": "not-an-array"},
                )
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="ask_failed", content=invalid_error, is_error=True)],
        ),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="ask_ok",
                    name="ask_user_question",
                    input={
                        "question": valid_question,
                        "options": [
                            {"id": "skip_clarify", "label": "暂不补充，按默认配置出方案"},
                            {"id": "not_deploy", "label": "不是部署需求，暂不处理"},
                        ],
                    },
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id="ask_ok",
                    content=json.dumps(
                        {
                            "selected_id": "skip_clarify",
                            "selected_label": "暂不补充，按默认配置出方案",
                            "free_text": "",
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        ),
    ]
    console = Console(record=True, width=100)

    def replay_history(messages: list[Message]) -> None:
        tool_results = {}
        for message in messages:
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        tool_results[block.tool_use_id] = block
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    console.print(f"REPLAY:{block.name}")
                    result = tool_results.get(block.id)
                    if result is not None:
                        state = "ERROR" if result.is_error else "OK"
                        console.print(f"REPLAY:{state}:{result.content}")

    PipelineDisplayReplayRenderer(
        console,
        history_replayer=replay_history,
        transcript_loader=lambda _transcript_id: transcript,
    ).render(model)

    text = console.export_text()
    assert f"REPLAY:ERROR:{invalid_error}" in text
    assert f"> {invalid_error}" not in text
    assert text.count(valid_question) == 1
    assert "1. 暂不补充，按默认配置出方案" in text
    assert "> 暂不补充，按默认配置出方案" in text


def test_renderer_prints_repeated_sub_step_attempts():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="evaluate_candidates",
                attempt_no=1,
                status="interrupted",
                sub_pipelines={
                    "candidate_0": DisplaySubPipeline(
                        sub_pipeline_id="candidate_0",
                        candidate_name="低成本方案",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=1,
                                status="completed",
                            ),
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=2,
                                status="running",
                            ),
                        ],
                    )
                },
            )
        ],
    )

    text = _render_text(model)

    assert "Template generation: Completed" in text
    assert "Template generation #2: Running" in text


def test_renderer_does_not_duplicate_global_interrupted_status_when_attempt_is_interrupted():
    model = DisplayReplayModel(
        pipeline_name="selling",
        interrupted=True,
        attempts=[DisplayAttempt(step_id="architecture_planning", attempt_no=1, status="interrupted")],
    )

    text = _render_text(model)

    assert text.count("Interrupted") == 1


def test_renderer_replays_agent_loop_transcript_for_normal_step():
    output: list[str] = []
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="intent_parsing",
                attempt_no=1,
                index=1,
                total=5,
                status="completed",
                transcript_id="transcript_att_0001",
                tools=[DisplayToolUse(name="complete_step", tool_use_id="tu_1")],
            )
        ],
    )
    transcript = [
        Message(role="user", content="选择一个已有vpc，创建一个vswitch"),
        Message(
            role="assistant",
            content=[
                TextBlock(text="我会解析你的网络需求。"),
                ToolUseBlock(id="tu_1", name="complete_step", input={"conclusion": {"ok": True}}),
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="tu_1", content="步骤 intent_parsing 完成。结论已提交。")],
        ),
    ]

    def replay_history(messages: list[Message]) -> None:
        output.extend(message.role for message in messages)
        assert all(message.content != "选择一个已有vpc，创建一个vswitch" for message in messages)

    console = Console(record=True, width=100)
    PipelineDisplayReplayRenderer(
        console,
        history_replayer=replay_history,
        transcript_loader=lambda transcript_id: transcript if transcript_id == "transcript_att_0001" else [],
    ).render(model)

    assert output == ["assistant", "user"]
    assert "complete_step" not in console.export_text()


def test_renderer_prints_blank_line_after_step_header_before_transcript():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="intent_parsing",
                attempt_no=1,
                index=1,
                total=5,
                status="completed",
                transcript_id="transcript_att_0001",
            )
        ],
    )
    console = Console(record=True, width=100)

    def replay_history(_messages: list[Message]) -> None:
        console.print("TRANSCRIPT")

    PipelineDisplayReplayRenderer(
        console,
        history_replayer=replay_history,
        transcript_loader=lambda _transcript_id: [Message(role="assistant", content="TRANSCRIPT")],
    ).render(model)

    assert "● Intent parsing (1/5) \n\nTRANSCRIPT" in console.export_text()


def test_renderer_separates_consecutive_assistant_transcript_chunks():
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="deploying",
                attempt_no=1,
                status="completed",
                transcript_id="transcript_att_0007",
            )
        ],
    )
    transcript = [
        Message(role="assistant", content=[TextBlock(text="第一段")]),
        Message(role="assistant", content=[TextBlock(text="第二段")]),
    ]
    console = Console(record=True, width=100)

    def replay_history(messages: list[Message]) -> None:
        for message in messages:
            console.print(f"REPLAY:{message.get_text()}")

    PipelineDisplayReplayRenderer(
        console,
        history_replayer=replay_history,
        transcript_loader=lambda _transcript_id: transcript,
    ).render(model)

    assert "REPLAY:第一段\n\nREPLAY:第二段" in console.export_text()


def test_renderer_replays_agent_loop_transcript_for_sub_step():
    output: list[str] = []
    model = DisplayReplayModel(
        pipeline_name="selling",
        attempts=[
            DisplayAttempt(
                step_id="evaluate_candidates",
                attempt_no=1,
                sub_pipelines={
                    "candidate_0": DisplaySubPipeline(
                        sub_pipeline_id="candidate_0",
                        candidate_name="低成本方案",
                        steps=[
                            DisplaySubStepAttempt(
                                step_id="template_generating",
                                attempt_no=1,
                                status="completed",
                                transcript_id="transcript_att_0002",
                            )
                        ],
                    )
                },
            )
        ],
    )

    def replay_history(messages: list[Message]) -> None:
        output.extend(message.role for message in messages)

    PipelineDisplayReplayRenderer(
        Console(record=True, width=100),
        history_replayer=replay_history,
        transcript_loader=lambda transcript_id: [
            Message(role="user", content="请完成当前步骤：template_generating。"),
            Message(role="assistant", content=[TextBlock(text="生成模板完成。")]),
        ],
    ).render(model)

    assert output == ["assistant"]
