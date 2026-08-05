import asyncio
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


class _StringySecret:
    def __str__(self) -> str:
        return "object api_key=sk-object12345678"


def _aliyun_threshold_pair(limit: int, diagnostics: str = "") -> tuple[str, str]:
    marker = "BUSINESS_TAIL_MARKER"
    empty_body = json.dumps({"payload": "", "tail": marker}, ensure_ascii=False, indent=2)
    payload_size = limit - len(empty_body) - len(diagnostics)
    payload = {"payload": "X" * payload_size, "tail": marker}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    envelope = json.dumps(
        {
            "status": 200,
            "headers": {"requestid": "req-1"},
            "body": payload,
            "content_type": "application/json",
            "content_encoding": None,
            "size": len(body),
        },
        ensure_ascii=False,
        indent=2,
    )
    return body + diagnostics, envelope + diagnostics


def _run_reducer_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    events_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/events.js"
    api_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/api.js"
    app_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/app.js"
    tool_cards_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js"
    pipeline_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/pipeline.js"
    blocking_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/blocking.js"
    mermaid_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/mermaid_render.js"
    output_panel_js = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/output_panel.js"
    script = tmp_path / "reducer-test.mjs"
    script_source = (
        source.strip()
        .replace("__EVENTS_MODULE__", json.dumps(events_js.as_uri()))
        .replace("__API_MODULE__", json.dumps(api_js.as_uri()))
        .replace("__APP_MODULE__", json.dumps(app_js.as_uri()))
        .replace("__TOOL_CARDS_MODULE__", json.dumps(tool_cards_js.as_uri()))
        .replace("__PIPELINE_MODULE__", json.dumps(pipeline_js.as_uri()))
        .replace("__BLOCKING_MODULE__", json.dumps(blocking_js.as_uri()))
        .replace("__MERMAID_MODULE__", json.dumps(mermaid_js.as_uri()))
        .replace("__OUTPUT_PANEL_MODULE__", json.dumps(output_panel_js.as_uri()))
    )
    script.write_text(script_source, encoding="utf-8")

    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_translator_tool_started_payload_identity_and_status() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_started(
        tool_use_id="tool-1",
        tool_name="ros_stack",
        parent_tool_use_id="tool-parent",
    )

    assert event["type"] == "tool.started"
    assert event["sequence"] == 0
    assert event["sessionId"] == "session-1"
    assert event["payload"] == {
        "toolUseId": "tool-1",
        "toolName": "ros_stack",
        "parentToolUseId": "tool-parent",
        "status": "running",
    }


def test_normal_web_live_tool_result_strips_only_aliyun_internal_carrier() -> None:
    from iac_code.types.stream_events import ToolResultEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            result='{"Instances": []}',
            metadata={
                "aliyun_http": {
                    "contract_version": "aliyun_body_v1",
                    "status": 200,
                },
                "ros_validation": {"valid": True},
            },
        ),
        turn_id="turn-1",
    )

    assert translated["type"] == "tool.result"
    assert translated["payload"]["summary"] == '{"Instances": []}'
    assert translated["payload"]["artifacts"] == [{"ros_validation": {"valid": True}}]
    assert "aliyun_http" not in json.dumps(translated)


def test_normal_web_live_tool_result_strips_internal_render_carrier() -> None:
    # B1: the live (non-replay) path must match the persisted/replay path and hide the
    # internal render carrier (_iac_code_tool_render) from the frontend "Artifacts" area,
    # not just the aliyun_http key. Otherwise raw render metadata leaks as JSON noise.
    from iac_code.types.stream_events import TOOL_RENDER_METADATA_KEY, ToolResultEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="aliyun_api",
            result='{"Instances": []}',
            metadata={
                TOOL_RENDER_METADATA_KEY: {"display_name": "List instances"},
                "ros_validation": {"valid": True},
            },
        ),
        turn_id="turn-1",
    )

    assert translated["payload"]["artifacts"] == [{"ros_validation": {"valid": True}}]
    assert TOOL_RENDER_METADATA_KEY not in json.dumps(translated)


def test_normal_web_live_tool_result_render_only_metadata_yields_no_artifacts() -> None:
    # When the only metadata is the internal render carrier, artifacts must be empty
    # rather than a JSON blob of the carrier.
    from iac_code.types.stream_events import TOOL_RENDER_METADATA_KEY, ToolResultEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        ToolResultEvent(
            tool_use_id="tool-1",
            tool_name="bash",
            result="ok",
            metadata={TOOL_RENDER_METADATA_KEY: {"display_name": "Run"}},
        ),
        turn_id="turn-1",
    )

    assert translated["payload"]["artifacts"] == []


@pytest.mark.parametrize("diagnostics", ["", "\nDelegated diagnostics: preflight passed"])
def test_normal_web_live_preserves_result_storage_boundary_content(tmp_path, diagnostics) -> None:
    from iac_code.tools.result_storage import EXTERNALIZED_RESULT_PATH_METADATA_KEY, ResultStorage
    from iac_code.types.stream_events import ToolResultEvent
    from iac_code.web.events import WebEventTranslator

    new_content, old_content = _aliyun_threshold_pair(50_000, diagnostics)
    storage = ResultStorage(
        storage_dir=str(tmp_path / "tool-results"),
        max_inline_chars=50_000,
        preview_chars=2_000,
    )
    new_result = storage.process("new", new_content)
    old_result = storage.process("old", old_content)
    translator = WebEventTranslator("session-1")
    new_event = translator.translate_stream_event(
        ToolResultEvent(
            tool_use_id="new",
            tool_name="aliyun_api" if not diagnostics else "ros_validate_template",
            result=new_result.content,
            metadata={"aliyun_http": {"contract_version": "aliyun_body_v1", "content_state": "inline_final"}},
        ),
        turn_id="turn-1",
    )
    old_event = translator.translate_stream_event(
        ToolResultEvent(
            tool_use_id="old",
            tool_name="aliyun_api" if not diagnostics else "ros_validate_template",
            result=old_result.content,
            metadata={
                "aliyun_http": {
                    "contract_version": "aliyun_body_v1",
                    "content_state": "externalized_preview",
                },
                EXTERNALIZED_RESULT_PATH_METADATA_KEY: old_result.file_path,
            },
        ),
        turn_id="turn-1",
    )

    assert new_event["payload"]["summary"] == new_content
    assert new_event["payload"]["artifacts"] == []
    assert old_event["payload"]["summary"] == old_result.content
    assert old_event["payload"]["artifacts"] == [{EXTERNALIZED_RESULT_PATH_METADATA_KEY: old_result.file_path}]
    assert "aliyun_http" not in json.dumps([new_event, old_event])


def test_stream_event_translator_backend_event_names_match_contract() -> None:
    from iac_code.types.stream_events import (
        CandidateDetailEvent,
        CompactionEvent,
        DiagramEvent,
        ErrorEvent,
        PlanEvent,
        PlanStep,
        QueuedInputSubmittedEvent,
        ResourceObservedEvent,
        StackInstancesProgressEvent,
        StackProgressEvent,
        SubAgentToolEvent,
        SubPipelineStreamEvent,
        TaskNotificationEvent,
        TextDeltaEvent,
        ThinkingDeltaEvent,
        TombstoneEvent,
        ToolInputDeltaEvent,
        ToolResultEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")
    cases = [
        (ToolUseStartEvent(tool_use_id="tool-1", name="bash"), "tool.started"),
        (ToolInputDeltaEvent(tool_use_id="tool-1", partial_json='{"cmd":'), "tool.input.delta"),
        (ToolUseEndEvent(tool_use_id="tool-1", name="bash", input={"cmd": "echo hi"}), "tool.finished"),
        (ToolResultEvent(tool_use_id="tool-1", tool_name="bash", result="hi"), "tool.result"),
        (ThinkingDeltaEvent(text="considering"), "assistant.thinking.delta"),
        (QueuedInputSubmittedEvent(text="more detail"), "queued-input.submitted"),
        (TombstoneEvent(message_id="message-1"), "assistant.message.tombstone"),
        (
            TaskNotificationEvent(task_id="task-1", description="write tests", status="completed", result="ok"),
            "task.notification",
        ),
        (CompactionEvent(original_tokens=100, compacted_tokens=20), "compaction.finished"),
        (ErrorEvent(error="boom", is_retryable=False, error_id="err-1"), "error"),
        (
            SubAgentToolEvent(parent_tool_use_id="parent-1", child_tool_name="read_file", child_tool_input={}),
            "subagent.event",
        ),
        (
            ResourceObservedEvent(
                provider="aliyun",
                resource_type="ALIYUN::ECS::Instance",
                resource_id="i-123",
                resource_name="web",
                region_id="cn-hangzhou",
                action="create",
                tool_name="ros_stack",
                tool_use_id="tool-3",
                metadata={"stackId": "stack-1"},
            ),
            "resource.observed",
        ),
        (
            DiagramEvent(candidate_name="vpc", template_content="{}", mermaid_source="graph TD", candidate_index=0),
            "diagram.render",
        ),
        (
            CandidateDetailEvent(
                tool_use_id="tool-2",
                candidate_name="vpc",
                summary="small",
                cost_items=[],
                total_monthly_cost="$0",
                candidate_index=0,
            ),
            "candidate.detail",
        ),
        (
            StackProgressEvent(
                stack_id="stack-1",
                stack_name="demo",
                status="CREATE_IN_PROGRESS",
                progress_percentage=50.0,
                resources=[],
                elapsed_seconds=3,
            ),
            "pipeline.event",
        ),
        (
            StackInstancesProgressEvent(
                stack_group_name="group-1",
                operation_id="op-1",
                status="RUNNING",
                progress_percentage=50,
                instances=[],
                elapsed_seconds=3,
            ),
            "pipeline.event",
        ),
        (PlanEvent(steps=[PlanStep(content="Inspect", status="in_progress", priority="high")]), "plan.updated"),
        (
            SubPipelineStreamEvent(
                sub_pipeline_id="candidate-a",
                candidate_index=0,
                inner=TextDeltaEvent(text="candidate text"),
            ),
            "assistant.text.delta",
        ),
    ]

    assert [
        translator.translate_stream_event(stream_event, turn_id="turn-1")["type"] for stream_event, _event_type in cases
    ] == [event_type for _stream_event, event_type in cases]


def test_stack_operation_started_bridges_to_resource_observed() -> None:
    from iac_code.types.stream_events import StackOperationStartedEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        StackOperationStartedEvent(
            provider="ros",
            stack_id="stack-9",
            stack_name="demo",
            region_id="cn-hangzhou",
            action="DeleteStack",
            tool_name="ros_stack",
            tool_use_id="tool-9",
        ),
        turn_id="turn-1",
    )

    # Reuses the same SSE the frontend already consumes so *_IN_PROGRESS shows at t0.
    assert translated["type"] == "resource.observed"
    payload = translated["payload"]
    assert payload["turnId"] == "turn-1"
    assert payload["provider"] == "ros"
    assert payload["resourceType"] == "stack"
    assert payload["resourceId"] == "stack-9"
    assert payload["resourceName"] == "demo"
    assert payload["regionId"] == "cn-hangzhou"
    assert payload["action"] == "DeleteStack"
    assert payload["toolName"] == "ros_stack"
    assert payload["toolUseId"] == "tool-9"
    assert payload["metadata"] == {}


def test_mcp_progress_translates_to_stable_tool_progress_event() -> None:
    from iac_code.types.stream_events import MCPProgressEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        MCPProgressEvent(
            server_name="ros-server",
            tool_name="preview_stack",
            progress=2,
            total=5,
            message="Validating",
            tool_use_id="tool-1",
            public_name="ROS PreviewStack",
        ),
        turn_id="turn-1",
    )

    assert translated["type"] == "tool.progress"
    assert translated["payload"] == {
        "turnId": "turn-1",
        "status": "progress",
        "toolUseId": "tool-1",
        "publicName": "ROS PreviewStack",
        "originalServerName": "ros-server",
        "originalToolName": "preview_stack",
        "progress": 2,
        "total": 5,
        "message": "Validating",
    }


def test_stack_progress_translation_carries_tool_use_id() -> None:
    # StackProgressEvent 现在带 tool_use_id,SSE 载荷必须透出 toolUseId,
    # 前端才能把进度挂到发起该栈操作的工具卡(normal 模式实时进度)。
    from iac_code.types.stream_events import StackProgressEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        StackProgressEvent(
            stack_id="stack-abc",
            stack_name="my-stack",
            status="CREATE_IN_PROGRESS",
            progress_percentage=42.0,
            resources=[{"name": "vpc", "resource_type": "ALIYUN::ECS::VPC", "status": "CREATE_IN_PROGRESS"}],
            elapsed_seconds=12,
            tool_use_id="tool-9",
        ),
        turn_id="turn-1",
    )

    assert translated["type"] == "pipeline.event"
    assert translated["payload"]["kind"] == "stack.progress"
    assert translated["payload"]["toolUseId"] == "tool-9"
    assert translated["payload"]["stackId"] == "stack-abc"


def test_stack_instances_progress_translation_carries_tool_use_id() -> None:
    from iac_code.types.stream_events import StackInstancesProgressEvent
    from iac_code.web.events import WebEventTranslator

    translated = WebEventTranslator("session-1").translate_stream_event(
        StackInstancesProgressEvent(
            stack_group_name="group-x",
            operation_id="op-1",
            status="RUNNING",
            progress_percentage=50,
            instances=[{"account_id": "1", "region_id": "cn-hangzhou", "status": "RUNNING"}],
            elapsed_seconds=8,
            tool_use_id="tool-10",
        ),
        turn_id="turn-1",
    )

    assert translated["type"] == "pipeline.event"
    assert translated["payload"]["kind"] == "stack.instances.progress"
    assert translated["payload"]["toolUseId"] == "tool-10"


def test_compaction_started_phase_translates_to_started_with_auto_flag() -> None:
    from iac_code.types.stream_events import CompactionEvent
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    started = translator.translate_stream_event(CompactionEvent(phase="started"), turn_id="turn-1")
    assert started["type"] == "compaction.started"
    assert started["payload"]["auto"] is True
    assert started["payload"]["state"] == "started"

    finished = translator.translate_stream_event(
        CompactionEvent(original_tokens=1200, compacted_tokens=400), turn_id="turn-1"
    )
    assert finished["type"] == "compaction.finished"
    # Default phase is "finished"; payload carries the token keys (values pass
    # through the shared secret-redaction pass, which is irrelevant to the UI).
    assert set(finished["payload"]) == {"originalTokens", "compactedTokens"}

    failed = translator.translate_stream_event(CompactionEvent(phase="failed", reason="no_result"), turn_id="turn-1")
    assert failed["type"] == "compaction.finished"
    assert failed["payload"] == {"auto": True, "state": "failed", "reason": "no_result"}


def test_encode_sse_uses_non_native_event_name_for_business_errors() -> None:
    from iac_code.web.events import encode_sse

    event = {
        "type": "error",
        "sequence": 7,
        "sessionId": "session-1",
        "payload": {"message": "boom"},
    }

    encoded = encode_sse(event)

    assert encoded.startswith("event: app.error\n")
    assert "\nid: 7\n" in encoded
    assert '"type":"error"' in encoded
    assert '"message":"boom"' in encoded


def test_frontend_reducer_unwraps_pipeline_snapshot_event_payload(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            const snapshot = {
              contextId: "ctx-1",
              waitingInput: { kind: "candidateSelection" },
            };
            const state = reduceEvent({}, {
              type: "pipeline.snapshot",
              sequence: 1,
              payload: { snapshot },
            });

            console.log(JSON.stringify({ pipelineSnapshot: state.pipelineSnapshot }));
            """
        ),
    )

    assert output["pipelineSnapshot"] == {
        "contextId": "ctx-1",
        "waitingInput": {"kind": "candidateSelection"},
    }


def test_frontend_turn_done_clears_abandoned_compaction_running_state(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "compaction.started",
              sequence: 1,
              payload: { auto: true },
            });
            state = reduceEvent(state, {
              type: "turn.done",
              sequence: 2,
              payload: { turnId: "turn-1", canceled: true },
            });

            console.log(JSON.stringify({ compaction: state.compaction }));
            """
        ),
    )

    assert output["compaction"] == {"status": "completed", "auto": True, "state": "canceled"}


def test_frontend_reducer_and_tool_cards_render_local_shell_events(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [], shellIds: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              if (node.dataset?.toolUseId) {
                result.shellIds.push(node.dataset.toolUseId);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            let state = reduceEvent({}, {
              type: "local.shell.start",
              sequence: 1,
              payload: {
                command: "curl https://example.com",
                local: true,
                entersAgentContext: false,
              },
            });
            state = reduceEvent(state, {
              type: "local.shell.end",
              sequence: 2,
              payload: {
                command: "curl https://example.com",
                exitCode: 7,
                stdout: "partial",
                stderr: "connection failed",
                local: true,
                entersAgentContext: false,
              },
            });

            const rendered = collect(renderToolCards(state));
            console.log(JSON.stringify({
              localShell: state.localShell,
              rendered,
            }));
            """
        ),
    )

    local_shell = list(output["localShell"].values())
    assert local_shell == [
        {
            "toolUseId": "local-shell-1",
            "toolName": "Local shell",
            "command": "curl https://example.com",
            "status": "failed",
            "exitCode": 7,
            "stdout": "partial",
            "stderr": "connection failed",
            "local": True,
            "entersAgentContext": False,
        }
    ]
    assert "local-shell-1" in output["rendered"]["shellIds"]
    # exitCode 7 → reducer 标记 status="failed"，标题体现失败（Issue 4）。
    assert "Run failed: curl https://example.com" in output["rendered"]["text"]
    assert "Shell" in output["rendered"]["text"]
    assert "$ curl https://example.com" in output["rendered"]["text"]
    assert "Exit code 7" in output["rendered"]["text"]
    assert "partial" in output["rendered"]["text"]
    assert "connection failed" in output["rendered"]["text"]
    assert "tool-card-local" in " ".join(output["rendered"]["classNames"])


def test_frontend_stack_progress_attaches_to_tool_card(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "tool-1", toolName: "ros_stack" },
            });
            // 带 toolUseId 的栈进度 → 应挂到 tool-1 的卡上。
            state = reduceEvent(state, {
              type: "pipeline.event",
              sequence: 2,
              payload: {
                kind: "stack.progress",
                toolUseId: "tool-1",
                stackName: "my-stack",
                stackId: "stack-abc",
                status: "DELETE_IN_PROGRESS",
                progressPercentage: 40,
                resources: [
                  { name: "my-vpc", resource_type: "ALIYUN::ECS::VPC", status: "DELETE_IN_PROGRESS" },
                ],
                elapsedSeconds: 12,
                deploymentComplete: false,
              },
            });
            // 无 toolUseId 的事件 → 不得凭空造出新工具卡(只进 pipelineEvents)。
            state = reduceEvent(state, {
              type: "pipeline.event",
              sequence: 3,
              payload: { kind: "stack.progress", stackName: "orphan", status: "CREATE_IN_PROGRESS" },
            });

            const rendered = collect(renderToolCards(state));
            console.log(JSON.stringify({
              stackProgress: state.tools["tool-1"].stackProgress,
              toolStatus: state.tools["tool-1"].status,
              toolCount: Object.keys(state.tools).length,
              pipelineEventCount: state.pipelineEvents.length,
              rendered,
            }));
            """
        ),
    )

    assert output["stackProgress"]["stackName"] == "my-stack"
    assert output["stackProgress"]["progressPercentage"] == 40
    # 未终态 → reducer 把工具标为 running(卡标题走进行中态)。
    assert output["toolStatus"] == "running"
    # 无 toolUseId 的事件不建卡:仍只有 tool-1 这一个工具。
    assert output["toolCount"] == 1
    # 两条 pipeline.event 都进了面板流(pipeline 模式渲染不受影响)。
    assert output["pipelineEventCount"] == 2
    text_blob = " ".join(output["rendered"]["text"])
    assert "Stack: my-stack" in text_blob
    assert "40%" in text_blob
    # 状态码经 CONCLUSION_VALUE_LABELS 翻中文。
    assert "Deleting" in text_blob
    assert "my-vpc" in text_blob
    assert "Elapsed 12s" in text_blob
    assert "tool-stack-progress" in " ".join(output["rendered"]["classNames"])


def test_frontend_stack_progress_renders_table_with_status_coloring(tmp_path) -> None:
    # 问题 1:资源进度改用表格(列头 + 每行一格),状态列按 完成/失败 上色;
    # 问题 2:进行中帧的「已用 N 秒」写 data-* 基准(供心跳每秒续算)。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function walk(node, out) {
              out.tags.push(node.tagName);
              if (node.textContent) out.text.push(node.textContent);
              if (node.className) out.classNames.push(node.className);
              if (node.className === "tool-stack-progress-meta") {
                out.metaDataset = { ...node.dataset };
              }
              for (const child of node.children || []) walk(child, out);
              return out;
            }

            globalThis.document = { createElement(tagName) { return new Element(tagName); } };

            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "tool-1", toolName: "ros_stack" },
            });
            state = reduceEvent(state, {
              type: "pipeline.event",
              sequence: 2,
              payload: {
                kind: "stack.progress",
                toolUseId: "tool-1",
                stackName: "my-stack",
                status: "CREATE_IN_PROGRESS",
                progressPercentage: 50,
                resources: [
                  { name: "vpc-a", resource_type: "ALIYUN::ECS::VPC", status: "CREATE_COMPLETE" },
                  { name: "eip-b", resource_type: "ALIYUN::VPC::EIP", status: "CREATE_FAILED" },
                ],
                elapsedSeconds: 20,
                deploymentComplete: false,
              },
            });

            const out = walk(renderToolCards(state), { tags: [], text: [], classNames: [], metaDataset: null });
            console.log(JSON.stringify(out));
            """
        ),
    )

    assert "TABLE" in output["tags"]
    assert "TH" in output["tags"]
    text_blob = " ".join(output["text"])
    # 列头 + 单元格文本齐备。
    assert "Resource" in text_blob
    assert "Type" in text_blob
    assert "Status" in text_blob
    assert "vpc-a" in text_blob
    assert "eip-b" in text_blob
    class_blob = " ".join(output["classNames"])
    # 状态列按行上色:一行完成、一行失败。
    assert "tool-stack-progress-cell-status is-done" in class_blob
    assert "tool-stack-progress-cell-status is-error" in class_blob
    # 进行中帧:meta 带 data-* 基准供心跳续算。
    assert output["metaDataset"]["stackElapsedBase"] == "20"
    assert output["metaDataset"]["stackReceivedAt"]


def test_frontend_active_stack_progress_card_auto_expands_in_pipeline(tmp_path) -> None:
    # Issue 2a:流水线(collapseNonComplete)里部署进行中的工具卡默认收起,看不到实时进度。
    # 挂上实时栈进度帧且仍在进行中 → 该卡必须自动展开;无进度帧的进行中卡仍保持收起。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            function findCard(node, toolUseId) {
              if (
                typeof node.className === "string" &&
                node.className.includes("tool-card") &&
                node.dataset &&
                node.dataset.toolUseId === toolUseId
              ) {
                return node;
              }
              for (const child of node.children || []) {
                const found = findCard(child, toolUseId);
                if (found) {
                  return found;
                }
              }
              return null;
            }

            // tool-with-progress:部署进行中且已有实时进度帧。
            let withProgress = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "deploy-1", toolName: "ros_stack" },
            });
            withProgress = reduceEvent(withProgress, {
              type: "pipeline.event",
              sequence: 2,
              payload: {
                kind: "stack.progress",
                toolUseId: "deploy-1",
                stackName: "my-stack",
                status: "CREATE_IN_PROGRESS",
                progressPercentage: 60,
                resources: [],
                elapsedSeconds: 5,
                deploymentComplete: false,
              },
            });
            const cardWith = findCard(renderToolCards(withProgress, { collapseNonComplete: true }), "deploy-1");

            // tool-without-progress:同样进行中但没有进度帧 → 保持收起。
            const noProgress = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "deploy-2", toolName: "ros_stack" },
            });
            const cardWithout = findCard(renderToolCards(noProgress, { collapseNonComplete: true }), "deploy-2");

            console.log(JSON.stringify({
              withProgressOpen: cardWith ? cardWith.open === true : null,
              withoutProgressOpen: cardWithout ? cardWithout.open === true : null,
            }));
            """
        ),
    )

    assert output["withProgressOpen"] is True
    assert output["withoutProgressOpen"] is False


def test_frontend_active_stack_progress_group_auto_expands_when_collapsed(tmp_path) -> None:
    # Issue 2b:部署进行中的 ros_stack 若与别的工具同处一条助手消息,会被折进「工具组」。
    # collapseNonComplete(如流水线切回 normal chat 仍带 contextId 的会话)下整组强制收起,
    # 即便组内那张 ros_stack 卡自身已展开,外层收起的 <details> 仍把实时进度藏了起来。
    # 组内只要有一张「进行中且已挂实时进度帧」的栈卡,整组必须自动展开(先于 collapseNonComplete)。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            function findByClass(node, cls) {
              if (typeof node.className === "string" && node.className.includes(cls)) {
                return node;
              }
              for (const child of node.children || []) {
                const found = findByClass(child, cls);
                if (found) {
                  return found;
                }
              }
              return null;
            }

            function findCard(node, toolUseId) {
              if (
                typeof node.className === "string" &&
                node.className.includes("tool-card") &&
                node.dataset &&
                node.dataset.toolUseId === toolUseId
              ) {
                return node;
              }
              for (const child of node.children || []) {
                const found = findCard(child, toolUseId);
                if (found) {
                  return found;
                }
              }
              return null;
            }

            // 一条助手消息里的两个工具:ros_stack(进行中 + 实时进度)与另一进行中工具 →
            // grouped 渲染时二者进同一「工具组」。
            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "deploy-1", toolName: "ros_stack" },
            });
            state = reduceEvent(state, {
              type: "tool.started",
              sequence: 2,
              payload: { toolUseId: "read-1", toolName: "read_file" },
            });
            state = reduceEvent(state, {
              type: "pipeline.event",
              sequence: 3,
              payload: {
                kind: "stack.progress",
                toolUseId: "deploy-1",
                stackName: "my-stack",
                status: "CREATE_IN_PROGRESS",
                progressPercentage: 40,
                resources: [],
                elapsedSeconds: 5,
                deploymentComplete: false,
              },
            });

            const root = renderToolCards(state, { grouped: true, collapseNonComplete: true });
            const group = findByClass(root, "tool-group");
            const card = findCard(root, "deploy-1");
            console.log(JSON.stringify({
              groupFound: !!group,
              groupOpen: group ? group.open === true : null,
              cardOpen: card ? card.open === true : null,
            }));
            """
        ),
    )

    assert output["groupFound"] is True
    # 组内有进行中的栈进度 → 整组自动展开,实时进度不再被收起的分组藏起来。
    assert output["groupOpen"] is True
    assert output["cardOpen"] is True


def test_frontend_tool_cards_group_multiple_shell_commands(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [], shellIds: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              if (node.dataset?.toolUseId) {
                result.shellIds.push(node.dataset.toolUseId);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const state = {
              localShell: {
                "shell-1": {
                  toolUseId: "shell-1",
                  command: "ls -d .worktrees",
                  status: "completed",
                  exitCode: 0,
                  stdout: "",
                  stderr: "",
                  local: true,
                },
                "shell-2": {
                  toolUseId: "shell-2",
                  command: "git show-ref --verify --quiet refs/heads/feature-web",
                  status: "completed",
                  exitCode: 1,
                  stdout: "",
                  stderr: "",
                  local: true,
                },
                "shell-3": {
                  toolUseId: "shell-3",
                  command: "git check-ignore -q .worktrees",
                  status: "completed",
                  exitCode: 0,
                  stdout: "ignored=0",
                  stderr: "",
                  local: true,
                },
                "shell-4": {
                  toolUseId: "shell-4",
                  command: "git rev-parse --show-toplevel",
                  status: "completed",
                  exitCode: 0,
                  stdout: "/repo",
                  stderr: "",
                  local: true,
                },
              },
            };

            console.log(JSON.stringify(collect(renderToolCards(state, { grouped: true }))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_classes = " ".join(output["classNames"])
    assert "Ran 4 commands" in rendered_text
    assert "Ran ls -d .worktrees" in rendered_text
    assert "Ran git show-ref --verify --quiet refs/heads/feature-web" in rendered_text
    assert "tool-group" in rendered_classes
    assert "tool-group-summary" in rendered_classes
    assert "tool-group-list" in rendered_classes
    assert output["shellIds"] == ["shell-1", "shell-2", "shell-3", "shell-4"]


def test_frontend_tool_cards_group_aliyun_api_separately_and_show_product_action(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [], toolIds: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              if (node.dataset?.toolUseId) {
                result.toolIds.push(node.dataset.toolUseId);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const state = {
              tools: {
                "api-1": {
                  toolUseId: "api-1",
                  toolName: "aliyun_api",
                  input: { product: "ros", action: "ListStacks" },
                  status: "completed",
                },
                "api-2": {
                  toolUseId: "api-2",
                  toolName: "aliyun_api",
                  input: { Product: "ecs", Action: "DescribeInstances" },
                  status: "completed",
                },
              },
              localShell: {
                "shell-1": {
                  toolUseId: "shell-1",
                  command: "cat template.yml",
                  status: "completed",
                  exitCode: 0,
                  stdout: "",
                  stderr: "",
                  local: true,
                },
              },
            };

            console.log(JSON.stringify(collect(renderToolCards(state, { grouped: true }))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_classes = " ".join(output["classNames"])
    assert "Ran 1 command, called 2 Alibaba Cloud APIs" in rendered_text
    assert "used 2 tools" not in rendered_text
    assert "Called ROS ListStacks" in rendered_text
    assert "Called ECS DescribeInstances" in rendered_text
    assert "tool-card-aliyun-api" in rendered_classes
    assert output["toolIds"] == ["api-1", "api-2", "shell-1"]


def test_frontend_tool_cards_render_single_shell_without_outer_group(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const state = {
              localShell: {
                "shell-1": {
                  toolUseId: "shell-1",
                  command: "echo hi",
                  status: "completed",
                  exitCode: 0,
                  stdout: "hi",
                  stderr: "",
                  local: true,
                },
              },
            };

            console.log(JSON.stringify(collect(renderToolCards(state, { grouped: true }))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_classes = " ".join(output["classNames"])
    assert "tool-group" not in rendered_classes
    assert "Ran echo hi" in rendered_text
    assert "Shell" in rendered_text
    assert "$ echo hi" in rendered_text
    assert "hi" in rendered_text
    assert "✓ Success" in rendered_text


def test_frontend_tool_cards_render_generic_input_results_artifacts_children_and_elapsed(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "tool-1", toolName: "write_file", status: "running" },
            });
            state = reduceEvent(state, {
              type: "tool.input.delta",
              sequence: 2,
              payload: { toolUseId: "tool-1", delta: "{\\"path\\":\\"out.txt\\",\\"content\\":\\"hello\\"}" },
            });
            state = reduceEvent(state, {
              type: "tool.result",
              sequence: 3,
              payload: {
                toolUseId: "tool-1",
                resultKind: "text",
                summary: "listed files",
                artifacts: [{ path: "out.txt" }],
              },
            });
            state = reduceEvent(state, {
              type: "tool.started",
              sequence: 4,
              payload: { toolUseId: "tool-child", toolName: "read", parentToolUseId: "tool-1" },
            });
            state = reduceEvent(state, {
              type: "tool.finished",
              sequence: 5,
              payload: { toolUseId: "tool-1", status: "completed", elapsedMs: 42, summary: "done" },
            });

            console.log(JSON.stringify(collect(renderToolCards(state))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    assert "Modified out.txt" in rendered_text
    assert "42ms" in rendered_text
    assert "Input" in rendered_text
    assert "out.txt" in rendered_text
    assert "Result" in rendered_text
    assert "Artifacts" in rendered_text
    assert "out.txt" in rendered_text
    assert "Sub-tools" in rendered_text
    assert "tool-child" in rendered_text


def test_frontend_tool_cards_parse_stored_bash_result_into_shell_detail(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "tool-1", toolName: "bash", status: "running" },
            });
            state = reduceEvent(state, {
              type: "tool.input.delta",
              sequence: 2,
              payload: { toolUseId: "tool-1", delta: "{\\"command\\":\\"ls -lah\\"}" },
            });
            state = reduceEvent(state, {
              type: "tool.result",
              sequence: 3,
              payload: {
                toolUseId: "tool-1",
                resultKind: "text",
                content: "STDOUT:\\ntotal 8\\nfile.txt\\n\\nExit code: 0",
              },
            });
            state = reduceEvent(state, {
              type: "tool.finished",
              sequence: 4,
              payload: { toolUseId: "tool-1", status: "completed" },
            });

            console.log(JSON.stringify(collect(renderToolCards(state))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    assert "Ran ls -lah" in rendered_text
    assert "Shell" in rendered_text
    assert "$ ls -lah" in rendered_text
    assert "total 8\nfile.txt" in rendered_text
    assert "✓ Success" in rendered_text
    assert "No output" not in rendered_text
    assert "Result" not in rendered_text


def test_pipeline_workspace_renders_raw_recovered_a2a_events(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              addEventListener() {}
              set innerHTML(value) {
                this._innerHTML = value;
              }
              get innerHTML() {
                return this._innerHTML || "";
              }
              set colSpan(value) {
                this._colSpan = value;
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const rendered = collect(renderPipelineWorkspace({
              pipelineSnapshot: {
                contextId: "ctx-1",
                taskId: "task-1",
                pipelineName: "selling",
                lastSequence: 1,
                cleanup: { status: "none", resourceCount: 0, resources: [] },
              },
              pipelineEvents: [
                {
                  eventType: "stack_current_changed",
                  data: {
                    stackId: "stack-raw",
                    stackName: "Raw Stack",
                    stackStatus: "UPDATE_COMPLETE",
                    progress: 91,
                    regionId: "cn-shanghai",
                  },
                },
                {
                  eventType: "cleanup_progress",
                  data: {
                    status: "failed",
                    resourceCount: 2,
                    resources: [{ resourceId: "res-raw", cleanupStatus: "DELETE_FAILED" }],
                  },
                },
                {
                  eventType: "pipeline_handoff_ready",
                  data: {
                    targetNormalMode: "normal",
                    outcome: "ready",
                    summary: "handoff raw",
                  },
                },
                {
                  eventType: "candidate_selected",
                  data: { candidateName: "Recovered Plan", candidateIndex: 1 },
                },
              ],
              candidateDetails: [
                { candidateName: "Recovered Plan", candidateIndex: 1, summary: "selected from raw event" },
              ],
            }));
            console.log(JSON.stringify(rendered));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    assert "pipeline-progress-item" in " ".join(output["classNames"])
    assert "Raw Stack" in rendered_text
    assert "UPDATE_COMPLETE" in rendered_text
    assert "failed · 2 resources" in rendered_text
    assert "res-raw" in rendered_text
    assert "handoff raw" in rendered_text
    assert "Recovered Plan" in rendered_text
    assert "Selected" in rendered_text


def test_pipeline_candidate_selection_posts_session_candidate_and_overrides(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { selectPipelineCandidate } from __API_MODULE__;
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.type = "";
                this.value = "";
                this.disabled = false;
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              async click() {
                for (const handler of this.listeners.click || []) {
                  await handler({ preventDefault() {} });
                }
              }
              set innerHTML(value) {
                this._innerHTML = value;
              }
              get innerHTML() {
                return this._innerHTML || "";
              }
              set colSpan(value) {
                this._colSpan = value;
              }
            }

            function findTag(node, tagName) {
              if (node.tagName === tagName) {
                return node;
              }
              for (const child of node.children || []) {
                const result = findTag(child, tagName);
                if (result) {
                  return result;
                }
              }
              return null;
            }

            function findButton(node, label) {
              if (node.tagName === "BUTTON" && node.textContent === label) {
                return node;
              }
              for (const child of node.children || []) {
                const result = findButton(child, label);
                if (result) {
                  return result;
                }
              }
              return null;
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = { location: { origin: "http://localhost" } };
            const calls = [];
            globalThis.fetch = async (url, options) => {
              calls.push({ url, options });
              return {
                ok: true,
                status: 202,
                headers: { get: () => "application/json" },
                json: async () => ({ accepted: true, action: "select_candidate" }),
              };
            };

            const rendered = renderPipelineWorkspace(
              {
                currentSessionId: "web-session-1",
                pipelineSnapshot: {
                  contextId: "ctx-1",
                  display: {
                    candidateDetails: [
                      { candidateName: "Plan A", candidateIndex: 0, summary: "small ecs" },
                    ],
                  },
                },
              },
              { onSelectCandidate: selectPipelineCandidate },
            );
            findTag(rendered, "TEXTAREA").value = "{\\"InstanceType\\":\\"ecs.g7.large\\"}";
            await findButton(rendered, "Select candidate").click();

            console.log(JSON.stringify({
              calls: calls.map((call) => ({
                url: call.url,
                method: call.options.method,
                body: JSON.parse(call.options.body),
              })),
              rendered: collect(rendered),
            }));
            """
        ),
    )

    assert output["calls"] == [
        {
            "url": "/api/pipeline/candidates/select",
            "method": "POST",
            "body": {
                "sessionId": "web-session-1",
                "candidateName": "Plan A",
                "candidateIndex": 0,
                "parameterOverrides": {"InstanceType": "ecs.g7.large"},
            },
        }
    ]
    assert "accepted" in " ".join(output["rendered"]["text"])


def test_pipeline_duplicate_candidate_names_select_only_matching_index(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.disabled = false;
              }
              append(...children) { this.children.push(...children); }
              replaceChildren(...children) { this.children = children; }
              addEventListener(type, handler) { this.listeners[type] = handler; }
              set innerHTML(value) { this._innerHTML = value; }
              get innerHTML() { return this._innerHTML || ""; }
              set colSpan(value) { this._colSpan = value; }
            }

            function descendants(node, tagName, result = []) {
              if (node.tagName === tagName) result.push(node);
              for (const child of node.children || []) descendants(child, tagName, result);
              return result;
            }

            globalThis.document = { createElement: (tagName) => new Element(tagName) };
            const rendered = renderPipelineWorkspace(
              {
                currentSessionId: "web-session-1",
                pipelineSelectedCandidate: { candidateName: "Same plan", candidateIndex: 1 },
                pipelineSnapshot: {
                  display: {
                    candidateDetails: [
                      { candidateName: "Same plan", candidateIndex: 0, summary: "first" },
                      { candidateName: "Same plan", candidateIndex: 1, summary: "second" },
                    ],
                  },
                },
              },
              { onSelectCandidate: async () => ({ accepted: true }) },
            );
            const articles = descendants(rendered, "ARTICLE");
            console.log(JSON.stringify(articles.map((article) => ({
              key: article.dataset.candidateKey,
              selected: article.className.includes("is-selected"),
              disabled: descendants(article, "BUTTON")[0].disabled,
            }))));
            """
        ),
    )

    assert output == [
        {"key": "0:Same plan", "selected": False, "disabled": False},
        {"key": "1:Same plan", "selected": True, "disabled": True},
    ]


def test_pipeline_candidate_selection_discards_stale_success_after_session_switch(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { createPipelineCandidateSelectionHandler } = await import(__APP_MODULE__);

            let state = { currentSessionId: "session-a" };
            let renders = 0;
            let resolveSelection;
            const calls = [];
            const pendingSelection = new Promise((resolve) => {
              resolveSelection = resolve;
            });
            const handler = createPipelineCandidateSelectionHandler({
              selectCandidate: async (payload) => {
                calls.push(payload);
                return pendingSelection;
              },
              getState: () => state,
              setState: (nextState) => {
                state = nextState;
              },
              renderState: () => {
                renders += 1;
              },
            });

            const pending = handler({
              sessionId: "session-a",
              candidateName: "Plan A",
              candidateIndex: 0,
              parameterOverrides: { InstanceType: "ecs.g7.large" },
            });
            state = { currentSessionId: "session-b", pipelineNotice: "B notice" };
            resolveSelection({ accepted: true, action: "select_candidate" });
            const result = await pending;

            console.log(JSON.stringify({ calls, renders, result, state }));
            """
        ),
    )

    assert output["calls"] == [
        {
            "sessionId": "session-a",
            "candidateName": "Plan A",
            "candidateIndex": 0,
            "parameterOverrides": {"InstanceType": "ecs.g7.large"},
        }
    ]
    assert output["result"] == {"accepted": True, "action": "select_candidate"}
    # 乐观更新会在 await 前(仍是 session-a 时)先渲染一次;await 解析后已切到 session-b,
    # 故丢弃迟到的成功态(不再 setState/renderState),最终 state 保持 session-b 不被污染。
    assert output["renders"] == 1
    assert output["state"] == {"currentSessionId": "session-b", "pipelineNotice": "B notice"}


def test_app_session_event_guard_drops_stale_generation_and_foreign_session(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { isCurrentSessionEvent } = await import(__APP_MODULE__);

            console.log(JSON.stringify({
              currentBare: isCurrentSessionEvent(
                { sessionId: "session-a", type: "assistant.text.delta" },
                ["ws-session-a", "session-a"],
                3,
                3,
              ),
              currentWeb: isCurrentSessionEvent(
                { sessionId: "ws-session-a", type: "error" },
                ["ws-session-a", "session-a"],
                3,
                3,
              ),
              staleGeneration: isCurrentSessionEvent(
                { sessionId: "session-a", type: "error" },
                ["ws-session-a", "session-a"],
                2,
                3,
              ),
              foreignSession: isCurrentSessionEvent(
                { sessionId: "session-b", type: "error" },
                ["ws-session-a", "session-a"],
                3,
                3,
              ),
              sessionless: isCurrentSessionEvent(
                { type: "session.resync.required" },
                ["ws-session-a", "session-a"],
                3,
                3,
              ),
            }));
            """
        ),
    )

    assert output == {
        "currentBare": True,
        "currentWeb": True,
        "staleGeneration": False,
        "foreignSession": False,
        "sessionless": True,
    }


def test_pipeline_selection_workspace_opens_only_for_unresolved_candidate_input(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { pipelineSelectionRequiresWorkspace } = await import(__APP_MODULE__);

            const waiting = {
              currentSession: { mode: "pipeline" },
              pipelineSnapshot: {
                status: "input-required",
                pendingInput: { kind: "candidate_selection", required: true },
                display: { candidateDetails: [{ candidateName: "Plan A", candidateIndex: 0 }] },
              },
            };
            console.log(JSON.stringify({
              waiting: pipelineSelectionRequiresWorkspace(waiting),
              normal: pipelineSelectionRequiresWorkspace({ ...waiting, currentSession: { mode: "normal" } }),
              selected: pipelineSelectionRequiresWorkspace({
                ...waiting,
                pipelineSelectedCandidate: { candidateName: "Plan A", candidateIndex: 0 },
              }),
              clarification: pipelineSelectionRequiresWorkspace({
                currentSession: { mode: "pipeline" },
                pipelineSnapshot: {
                  status: "input-required",
                  pendingInput: { kind: "ask_user_question", required: true },
                  display: { candidateDetails: [] },
                },
              }),
              completed: pipelineSelectionRequiresWorkspace({
                ...waiting,
                pipelineSnapshot: {
                  ...waiting.pipelineSnapshot,
                  status: "completed",
                  pendingInput: null,
                },
              }),
            }));
            """
        ),
    )

    assert output == {
        "waiting": True,
        "normal": False,
        "selected": False,
        "clarification": False,
        "completed": False,
    }


def test_pipeline_candidate_selection_discards_stale_error_after_session_switch(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { createPipelineCandidateSelectionHandler } = await import(__APP_MODULE__);

            let state = { currentSessionId: "session-a" };
            let renders = 0;
            let rejectSelection;
            const calls = [];
            const pendingSelection = new Promise((_resolve, reject) => {
              rejectSelection = reject;
            });
            const handler = createPipelineCandidateSelectionHandler({
              selectCandidate: async (payload) => {
                calls.push(payload);
                return pendingSelection;
              },
              getState: () => state,
              setState: (nextState) => {
                state = nextState;
              },
              renderState: () => {
                renders += 1;
              },
            });

            const pending = handler({
              sessionId: "session-a",
              candidateName: "Plan A",
              candidateIndex: 0,
              parameterOverrides: {},
            }).catch((error) => ({ message: error.message }));
            state = { currentSessionId: "session-b", pipelineNotice: "B notice" };
            rejectSelection(new Error("A failed"));
            const result = await pending;

            console.log(JSON.stringify({ calls, renders, result, state }));
            """
        ),
    )

    assert output["calls"] == [
        {
            "sessionId": "session-a",
            "candidateName": "Plan A",
            "candidateIndex": 0,
            "parameterOverrides": {},
        }
    ]
    assert output["result"] == {"message": "A failed"}
    # 乐观更新先渲染一次(session-a);reject 时已切到 session-b,丢弃迟到的失败态,
    # 最终 state 保持 session-b。
    assert output["renders"] == 1
    assert output["state"] == {"currentSessionId": "session-b", "pipelineNotice": "B notice"}


def test_pipeline_candidate_selection_marks_selected_optimistically_before_action_resolves(tmp_path) -> None:
    # Issue 1-live/3:选择方案后必须「立刻」打勾并隐藏方案按钮,不能等 action POST 返回——
    # 该 POST 会同步跑完整个部署(数分钟)。断言 await 前 pipelineSelectedCandidate 就已写入。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { createPipelineCandidateSelectionHandler } = await import(__APP_MODULE__);

            let state = { currentSessionId: "session-a" };
            let renders = 0;
            let resolveSelection;
            const pendingSelection = new Promise((resolve) => {
              resolveSelection = resolve;
            });
            const handler = createPipelineCandidateSelectionHandler({
              selectCandidate: async () => pendingSelection,
              getState: () => state,
              setState: (nextState) => {
                state = nextState;
              },
              renderState: () => {
                renders += 1;
              },
            });

            const pending = handler({
              sessionId: "session-a",
              candidateName: "经济型 ECS + RDS Serverless",
              candidateIndex: 1,
            });
            // action POST 尚未返回(部署仍在进行),此刻已选态必须已经生效。
            const optimistic = { selected: state.pipelineSelectedCandidate ?? null, renders };
            resolveSelection({ accepted: true, action: "select_candidate" });
            await pending;
            const final = {
              selected: state.pipelineSelectedCandidate ?? null,
              notice: state.pipelineNotice,
              renders,
            };

            console.log(JSON.stringify({ optimistic, final }));
            """
        ),
    )

    assert output["optimistic"]["selected"] == {
        "candidateName": "经济型 ECS + RDS Serverless",
        "candidateIndex": 1,
    }
    assert output["optimistic"]["renders"] == 1
    assert output["final"]["selected"] == {
        "candidateName": "经济型 ECS + RDS Serverless",
        "candidateIndex": 1,
    }
    assert output["final"]["notice"] == "accepted · select_candidate"
    assert output["final"]["renders"] == 2


def test_pipeline_candidate_selection_rolls_back_optimistic_selection_on_error(tmp_path) -> None:
    # 选择失败时必须回滚乐观已选态,否则失败后仍错误地显示对勾/隐藏按钮。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { createPipelineCandidateSelectionHandler } = await import(__APP_MODULE__);

            let state = { currentSessionId: "session-a" };
            let rejectSelection;
            const pendingSelection = new Promise((_resolve, reject) => {
              rejectSelection = reject;
            });
            const handler = createPipelineCandidateSelectionHandler({
              selectCandidate: async () => pendingSelection,
              getState: () => state,
              setState: (nextState) => {
                state = nextState;
              },
              renderState: () => {},
            });

            const pending = handler({
              sessionId: "session-a",
              candidateName: "Plan A",
              candidateIndex: 0,
            }).catch((error) => ({ message: error.message }));
            const optimistic = state.pipelineSelectedCandidate ?? null;
            rejectSelection(new Error("boom"));
            const result = await pending;
            const final = {
              selected: state.pipelineSelectedCandidate ?? null,
              error: state.pipelineActionError,
            };

            console.log(JSON.stringify({ optimistic, result, final }));
            """
        ),
    )

    assert output["optimistic"] == {"candidateName": "Plan A", "candidateIndex": 0}
    assert output["result"] == {"message": "boom"}
    # 回滚后无已选态(前值为空),错误信息写入 pipelineActionError。
    assert output["final"]["selected"] is None
    assert output["final"]["error"] == "boom"


def test_pipeline_candidate_selection_rejects_invalid_override_json_before_api_call(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.type = "";
                this.value = "";
                this.disabled = false;
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              async click() {
                for (const handler of this.listeners.click || []) {
                  await handler({ preventDefault() {} });
                }
              }
              set innerHTML(value) {
                this._innerHTML = value;
              }
              get innerHTML() {
                return this._innerHTML || "";
              }
              set colSpan(value) {
                this._colSpan = value;
              }
            }

            function findTag(node, tagName) {
              if (node.tagName === tagName) {
                return node;
              }
              for (const child of node.children || []) {
                const result = findTag(child, tagName);
                if (result) {
                  return result;
                }
              }
              return null;
            }

            function findButton(node, label) {
              if (node.tagName === "BUTTON" && node.textContent === label) {
                return node;
              }
              for (const child of node.children || []) {
                const result = findButton(child, label);
                if (result) {
                  return result;
                }
              }
              return null;
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            let calls = 0;
            const rendered = renderPipelineWorkspace(
              {
                currentSessionId: "web-session-1",
                pipelineSnapshot: {
                  contextId: "ctx-1",
                  display: {
                    candidateDetails: [
                      { candidateName: "Plan A", candidateIndex: 0, summary: "small ecs" },
                    ],
                  },
                },
              },
              {
                onSelectCandidate: async () => {
                  calls += 1;
                },
              },
            );
            findTag(rendered, "TEXTAREA").value = "{bad json";
            await findButton(rendered, "Select candidate").click();

            console.log(JSON.stringify({ calls, rendered: collect(rendered) }));
            """
        ),
    )

    assert output["calls"] == 0
    rendered_text = " ".join(output["rendered"]["text"])
    assert "Parameter overrides must be a valid JSON object." in rendered_text
    assert "pipeline-error" in " ".join(output["rendered"]["classNames"])


def test_pipeline_workspace_uses_text_content_for_malicious_snapshot_strings(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
                this.innerHTMLValues = [];
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              addEventListener() {}
              set innerHTML(value) {
                this.innerHTMLValues.push(value);
              }
              get innerHTML() {
                return this.innerHTMLValues.join("\\n");
              }
              set colSpan(value) {
                this._colSpan = value;
              }
            }

            function collect(node, result = { text: [], innerHTML: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.innerHTML) {
                result.innerHTML.push(node.innerHTML);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const malicious = "<img src=x onerror=alert(1)>";
            const rendered = collect(renderPipelineWorkspace({
              pipelineSnapshot: {
                contextId: "ctx-1",
                display: {
                  candidateDetails: [{ detail: { candidateName: malicious, summary: "safe summary" } }],
                  diagrams: [{ candidateName: malicious, mermaidSource: malicious }],
                  artifacts: [{ title: malicious }],
                },
                steps: [{ id: malicious, status: "working", candidates: [] }],
              },
            }));
            console.log(JSON.stringify(rendered));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_html = " ".join(output["innerHTML"])
    assert "<img src=x onerror=alert(1)>" in rendered_text
    assert "<img src=x onerror=alert(1)>" not in rendered_html


def test_open_event_stream_reports_async_handler_rejection_without_unhandled_rejection(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { openEventStream } from __API_MODULE__;

            const unhandled = [];
            globalThis.window = { location: { origin: "http://localhost" } };
            globalThis.EventSource = class {
              constructor(url) {
                this.url = url;
                this.handlers = {};
                globalThis.lastSource = this;
              }
              addEventListener(eventType, handler) {
                this.handlers[eventType] = handler;
              }
              removeEventListener(eventType) {
                delete this.handlers[eventType];
              }
              close() {
                this.closed = true;
              }
            };
            process.on("unhandledRejection", (error) => {
              unhandled.push(error instanceof Error ? error.message : String(error));
            });

            const seen = [];
            openEventStream("session-1", 0, async (event) => {
              seen.push(event.type);
              if (event.type !== "error") {
                throw new Error("async boom");
              }
            });
            globalThis.lastSource.handlers["assistant.text.delta"]({
              data: JSON.stringify({ type: "assistant.text.delta", sequence: 1, payload: { delta: "hi" } }),
            });
            await new Promise((resolve) => setTimeout(resolve, 0));
            console.log(JSON.stringify({ seen, unhandled }));
            """
        ),
    )

    assert output == {"seen": ["assistant.text.delta", "error"], "unhandled": []}


def test_open_event_stream_dispatches_app_error_transport_alias(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { openEventStream } from __API_MODULE__;

            globalThis.window = { location: { origin: "http://localhost" } };
            globalThis.EventSource = class {
              constructor(url) {
                this.url = url;
                this.handlers = {};
                globalThis.lastSource = this;
              }
              addEventListener(eventType, handler) {
                this.handlers[eventType] = handler;
              }
              removeEventListener(eventType) {
                delete this.handlers[eventType];
              }
              close() {
                this.closed = true;
              }
            };

            const seen = [];
            openEventStream("session-1", 0, (event) => {
              seen.push({ type: event.type, message: event.payload?.message });
            });
            const handler = globalThis.lastSource.handlers["app.error"];
            if (handler) {
              handler({
                data: JSON.stringify({
                  type: "error",
                  sequence: 3,
                  sessionId: "session-1",
                  payload: { message: "boom" },
                }),
              });
            }
            await new Promise((resolve) => setTimeout(resolve, 0));
            console.log(JSON.stringify({ hasHandler: Boolean(handler), seen }));
            """
        ),
    )

    assert output == {"hasHandler": True, "seen": [{"type": "error", "message": "boom"}]}


def test_pipeline_workspace_merges_cleanup_completed_resource_delta(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              set innerHTML(value) {
                this._innerHTML = value;
              }
              get innerHTML() {
                return this._innerHTML || "";
              }
              set colSpan(value) {
                this._colSpan = value;
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const rendered = collect(renderPipelineWorkspace({
              pipelineSnapshot: {
                contextId: "ctx-1",
                cleanup: {
                  status: "in_progress",
                  resourceCount: 1,
                  resources: [{ resourceId: "stack-123", regionId: "cn-hangzhou", stackStatus: "DELETE_IN_PROGRESS" }],
                },
              },
              pipelineEvents: [
                {
                  eventType: "cleanup_completed",
                  scope: "cleanup",
                  data: {
                    status: "completed",
                    resourceId: "stack-123",
                    regionId: "cn-hangzhou",
                    stackStatus: "DELETE_COMPLETE",
                  },
                },
              ],
            }));
            console.log(JSON.stringify(rendered));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    assert "completed · 1 resources" in rendered_text
    assert "stack-123" in rendered_text
    assert "DELETE_COMPLETE" in rendered_text
    assert "No cleanup resources." not in rendered_text


def test_pipeline_workspace_merges_cleanup_failed_resource_delta(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
              }
              append(...children) {
                this.children.push(...children);
              }
              set innerHTML(value) {
                this._innerHTML = value;
              }
              get innerHTML() {
                return this._innerHTML || "";
              }
              set colSpan(value) {
                this._colSpan = value;
              }
            }

            function collect(node, result = { text: [], classNames: [] }) {
              if (node.textContent) {
                result.text.push(node.textContent);
              }
              if (node.className) {
                result.classNames.push(node.className);
              }
              for (const child of node.children || []) {
                collect(child, result);
              }
              return result;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const rendered = collect(renderPipelineWorkspace({
              pipelineSnapshot: {
                contextId: "ctx-1",
                cleanup: {
                  status: "in_progress",
                  resourceCount: 2,
                  resources: [
                    { resourceId: "stack-a", regionId: "cn-hangzhou", stackStatus: "DELETE_COMPLETE" },
                    { resourceId: "stack-b", regionId: "cn-hangzhou", stackStatus: "DELETE_IN_PROGRESS" },
                  ],
                },
              },
              pipelineEvents: [
                {
                  eventType: "cleanup_failed",
                  scope: "cleanup",
                  data: {
                    status: "failed",
                    resourceId: "stack-b",
                    regionId: "cn-hangzhou",
                    cleanupStatus: "failed",
                    stackStatus: "DELETE_FAILED",
                    errorMessage: "delete failed",
                  },
                },
              ],
            }));
            console.log(JSON.stringify(rendered));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    assert "failed · 2 resources" in rendered_text
    assert "stack-a" in rendered_text
    assert "DELETE_COMPLETE" in rendered_text
    assert "stack-b" in rendered_text
    assert "DELETE_FAILED" in rendered_text
    assert "delete failed" in rendered_text


def test_frontend_reducer_pairs_local_shell_end_by_stable_id_for_duplicate_commands(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "local.shell.start",
              sequence: 1,
              payload: { shellUseId: "shell-a", command: "date" },
            });
            state = reduceEvent(state, {
              type: "local.shell.start",
              sequence: 2,
              payload: { shellUseId: "shell-b", command: "date" },
            });
            state = reduceEvent(state, {
              type: "local.shell.end",
              sequence: 3,
              payload: { shellUseId: "shell-a", command: "date", exitCode: 0, stdout: "first", stderr: "" },
            });
            state = reduceEvent(state, {
              type: "local.shell.end",
              sequence: 4,
              payload: { shellUseId: "shell-b", command: "date", exitCode: 0, stdout: "second", stderr: "" },
            });

            console.log(JSON.stringify(state.localShell));
            """
        ),
    )

    assert output["shell-a"]["stdout"] == "first"
    assert output["shell-b"]["stdout"] == "second"


def test_frontend_reducer_classifies_local_shell_canceled_and_permission_denied(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "local.shell.start",
              sequence: 1,
              payload: { shellUseId: "shell-cancel", command: "sleep 999" },
            });
            state = reduceEvent(state, {
              type: "local.shell.end",
              sequence: 2,
              payload: {
                shellUseId: "shell-cancel",
                command: "sleep 999",
                exitCode: 130,
                stdout: "",
                stderr: "Shell command canceled.",
              },
            });
            state = reduceEvent(state, {
              type: "local.shell.start",
              sequence: 3,
              payload: { shellUseId: "shell-denied", command: "rm file" },
            });
            state = reduceEvent(state, {
              type: "local.shell.end",
              sequence: 4,
              payload: {
                shellUseId: "shell-denied",
                command: "rm file",
                exitCode: 1,
                stdout: "",
                stderr: "Permission denied.",
              },
            });

            console.log(JSON.stringify(state.localShell));
            """
        ),
    )

    assert output["shell-cancel"]["status"] == "canceled"
    assert output["shell-cancel"]["reason"] == "canceled"
    assert output["shell-denied"]["status"] == "denied"
    assert output["shell-denied"]["reason"] == "permission_denied"


def test_stream_event_translator_maps_agent_loop_events_with_explicit_payloads() -> None:
    from iac_code.types.stream_events import (
        AskUserQuestionEvent,
        PlanEvent,
        PlanStep,
        QueuedInputSubmittedEvent,
        ResourceObservedEvent,
        SubPipelineStreamEvent,
        TextDeltaEvent,
    )
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    queued = translator.translate_stream_event(QueuedInputSubmittedEvent(text="queued follow-up"), turn_id="turn-1")
    plan = translator.translate_stream_event(
        PlanEvent(steps=[PlanStep(content="Create VPC", status="pending", priority="high")]),
        turn_id="turn-1",
    )
    resource = translator.translate_stream_event(
        ResourceObservedEvent(
            provider="aliyun",
            resource_type="ALIYUN::VPC::VPC",
            resource_id="vpc-123",
            resource_name="demo-vpc",
            region_id="cn-hangzhou",
            action="create",
            tool_name="ros_stack",
            tool_use_id="tool-1",
            metadata={"stackId": "stack-1"},
        ),
        turn_id="turn-1",
    )
    question = translator.translate_stream_event(
        AskUserQuestionEvent(
            tool_use_id="ask-1",
            question="Pick a zone",
            options=[{"id": "a", "label": "Zone A"}],
            allow_free_text=False,
            free_text_prompt="",
        ),
        turn_id="turn-1",
    )
    sub_pipeline = translator.translate_stream_event(
        SubPipelineStreamEvent(
            sub_pipeline_id="candidate-a",
            candidate_index=2,
            inner=TextDeltaEvent(text="candidate says hi"),
        ),
        turn_id="turn-1",
    )

    assert queued["type"] == "queued-input.submitted"
    assert queued["payload"] == {"turnId": "turn-1", "text": "queued follow-up"}
    assert plan["type"] == "plan.updated"
    assert plan["payload"] == {
        "turnId": "turn-1",
        "steps": [{"content": "Create VPC", "status": "pending", "priority": "high"}],
    }
    assert resource["type"] == "resource.observed"
    assert resource["payload"] == {
        "turnId": "turn-1",
        "provider": "aliyun",
        "resourceType": "ALIYUN::VPC::VPC",
        "resourceId": "vpc-123",
        "resourceName": "demo-vpc",
        "regionId": "cn-hangzhou",
        "action": "create",
        "toolName": "ros_stack",
        "toolUseId": "tool-1",
        "metadata": {"stackId": "stack-1"},
    }
    assert question["type"] == "question.request"
    assert question["payload"] == {
        "turnId": "turn-1",
        "toolUseId": "ask-1",
        "question": "Pick a zone",
        "options": [{"id": "a", "label": "Zone A"}],
        "allowFreeText": False,
        "freeTextPrompt": "",
    }
    assert sub_pipeline["type"] == "assistant.text.delta"
    assert sub_pipeline["payload"] == {
        "turnId": "turn-1",
        "messageId": "",
        "delta": "candidate says hi",
        "subPipelineId": "candidate-a",
        "candidateIndex": 2,
    }
    assert "debug.stream_event" not in {
        queued["type"],
        plan["type"],
        resource["type"],
        question["type"],
        sub_pipeline["type"],
    }


def test_sub_pipeline_message_start_does_not_replace_parent_message_id() -> None:
    from iac_code.types.stream_events import MessageStartEvent, SubPipelineStreamEvent, TextDeltaEvent
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    parent_start = translator.translate_stream_event(MessageStartEvent(message_id="parent-message"), turn_id="turn-1")
    child_start = translator.translate_stream_event(
        SubPipelineStreamEvent(
            sub_pipeline_id="candidate-a",
            candidate_index=0,
            inner=MessageStartEvent(message_id="child-message"),
        ),
        turn_id="turn-1",
    )
    parent_delta = translator.translate_stream_event(TextDeltaEvent(text="parent continues"), turn_id="turn-1")

    assert parent_start["payload"]["messageId"] == "parent-message"
    assert child_start["payload"]["messageId"] == "child-message"
    assert child_start["payload"]["subPipelineId"] == "candidate-a"
    assert parent_delta["payload"] == {
        "turnId": "turn-1",
        "messageId": "parent-message",
        "delta": "parent continues",
    }


def test_translator_tombstone_includes_affected_tool_use_ids() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tombstone(
        message_id="message-1",
        affected_tool_use_ids=["tool-1", "tool-2"],
    )

    assert event["type"] == "assistant.message.tombstone"
    assert event["payload"] == {
        "messageId": "message-1",
        "affectedToolUseIds": ["tool-1", "tool-2"],
    }


def test_stack_progress_event_payload_includes_region_progress_and_redacted_errors() -> None:
    from iac_code.types.stream_events import StackProgressEvent
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.translate_stream_event(
        StackProgressEvent(
            stack_id="stack-1",
            stack_name="demo-stack",
            status="CREATE_IN_PROGRESS",
            progress_percentage=40.0,
            resources=[
                {
                    "resourceId": "vpc-1",
                    "regionId": "cn-hangzhou",
                    "status": "CREATE_FAILED",
                    "statusReason": "api_key=sk-stack12345678 failed",
                }
            ],
            elapsed_seconds=12,
        ),
        turn_id="turn-1",
    )

    assert event["type"] == "pipeline.event"
    assert event["payload"] == {
        "kind": "stack.progress",
        "toolUseId": None,
        "stackId": "stack-1",
        "stackName": "demo-stack",
        "regionId": "cn-hangzhou",
        "status": "CREATE_IN_PROGRESS",
        "progress": 40.0,
        "progressPercentage": 40.0,
        "deploymentSucceeded": False,
        "deploymentComplete": False,
        "resources": [
            {
                "resourceId": "vpc-1",
                "regionId": "cn-hangzhou",
                "status": "CREATE_FAILED",
                "statusReason": "api_key=sk-stack12345678 failed",
            }
        ],
        "elapsedSeconds": 12,
    }


def test_stack_instances_progress_event_payload_includes_region_progress_and_local_errors() -> None:
    from iac_code.types.stream_events import StackInstancesProgressEvent
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.translate_stream_event(
        StackInstancesProgressEvent(
            stack_group_name="group-1",
            operation_id="op-1",
            status="RUNNING",
            progress_percentage=25,
            instances=[
                {
                    "stackId": "stack-i-1",
                    "regionId": "cn-shanghai",
                    "status": "OUTDATED",
                    "statusReason": "AccessKeySecret=LTAI123456789012 blocked",
                }
            ],
            elapsed_seconds=6,
        ),
        turn_id="turn-1",
    )

    assert event["type"] == "pipeline.event"
    assert event["payload"] == {
        "kind": "stack.instances.progress",
        "toolUseId": None,
        "stackGroupName": "group-1",
        "operationId": "op-1",
        "regionId": "cn-shanghai",
        "status": "RUNNING",
        "progress": 25,
        "progressPercentage": 25,
        "instances": [
            {
                "stackId": "stack-i-1",
                "regionId": "cn-shanghai",
                "status": "OUTDATED",
                "statusReason": "AccessKeySecret=LTAI123456789012 blocked",
            }
        ],
        "elapsedSeconds": 6,
    }


@pytest.mark.parametrize(
    ("status", "deployment_succeeded"),
    [
        ("CREATE_IN_PROGRESS", False),
        ("CHECK_COMPLETE", False),
        ("CREATE_COMPLETE", True),
    ],
)
def test_stack_progress_event_does_not_mark_deploy_success_until_create_complete(
    status: str,
    deployment_succeeded: bool,
) -> None:
    from iac_code.types.stream_events import StackProgressEvent
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.translate_stream_event(
        StackProgressEvent(
            stack_id="stack-1",
            stack_name="demo-stack",
            status=status,
            progress_percentage=100.0 if deployment_succeeded else 80.0,
            resources=[{"resourceId": "stack-1", "regionId": "cn-hangzhou", "action": "CreateStack"}],
            elapsed_seconds=30,
        ),
        turn_id="turn-1",
    )

    assert event["type"] == "pipeline.event"
    assert event["payload"]["kind"] == "stack.progress"
    assert event["payload"]["deploymentSucceeded"] is deployment_succeeded
    assert event["payload"]["deploymentComplete"] is deployment_succeeded


def test_stack_progress_event_region_from_event_field_when_resources_lack_region() -> None:
    # 生产中 base_stack 构造的 resources 只含 name/resource_type/status/status_reason,
    # 没有任何 region 字段,所以 _first_region_id(resources) 恒为 None。若栈操作事件本身不带
    # region,live overlay 的 regionId 会是空串,去重键 `::name` 与服务端 `region::name` 分裂,
    # 建栈期短暂出现两个同名栈。StackProgressEvent 必须自带权威 region_id 并透出到 SSE。
    from iac_code.types.stream_events import StackProgressEvent
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.translate_stream_event(
        StackProgressEvent(
            stack_id="stack-1",
            stack_name="single-vpc",
            status="CREATE_IN_PROGRESS",
            progress_percentage=40.0,
            resources=[
                {
                    "name": "vpc",
                    "resource_type": "ALIYUN::ECS::VPC",
                    "status": "CREATE_IN_PROGRESS",
                    "status_reason": "",
                }
            ],
            elapsed_seconds=12,
            region_id="cn-hangzhou",
        ),
        turn_id="turn-1",
    )

    assert event["type"] == "pipeline.event"
    assert event["payload"]["regionId"] == "cn-hangzhou"


def test_translator_assistant_text_delta_uses_delta_key() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.assistant_text_delta(message_id="message-1", delta="hello")

    assert event["type"] == "assistant.text.delta"
    assert event["payload"] == {
        "messageId": "message-1",
        "delta": "hello",
    }


def test_translator_tool_result_preserves_local_key_based_values() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_result(
        tool_use_id="tool-1",
        result_kind="artifact",
        summary={
            "message": "created template",
            "apiKey": "sk-unsafe",
        },
        artifacts=[
            {
                "path": "/tmp/template.yaml",
                "access_key_secret": "secret-value",
            }
        ],
    )

    assert event["type"] == "tool.result"
    assert event["payload"] == {
        "toolUseId": "tool-1",
        "resultKind": "artifact",
        "summary": {
            "message": "created template",
            "apiKey": "sk-unsafe",
        },
        "artifacts": [
            {
                "path": "/tmp/template.yaml",
                "access_key_secret": "secret-value",
            }
        ],
    }


def test_translator_tool_result_preserves_local_assignments_inside_strings() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_result(
        tool_use_id="tool-1",
        result_kind="text",
        summary="created with api_key=sk-real and token: abc",
        artifacts=[
            "access_key_secret=secret",
            {"raw": "token: abc"},
        ],
    )

    assert event["type"] == "tool.result"
    assert event["payload"]["summary"] == "created with api_key=sk-real and token: abc"
    assert event["payload"]["artifacts"] == ["access_key_secret=secret", {"raw": "token: abc"}]


def test_translator_tool_result_preserves_local_bare_string_values() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_result(
        tool_use_id="tool-1",
        result_kind="text",
        summary='plain sk-1234567890abcd and {"token":"abc.def.ghi"}',
        artifacts=[
            "Authorization: Bearer sk-abcdefgh12345678",
            "access key LTAI1234567890abcdef",
            {"raw": '{"api_key":"sk-json123456789"}'},
        ],
    )

    payload_text = json.dumps(event["payload"], sort_keys=True)
    assert "sk-1234567890abcd" in payload_text
    assert "abc.def.ghi" in payload_text
    assert "sk-abcdefgh12345678" in payload_text
    assert "LTAI1234567890abcdef" in payload_text
    assert "sk-json123456789" in payload_text


def test_translator_tool_result_preserves_local_cookie_bytes_and_fallback_strings() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_result(
        tool_use_id="tool-1",
        result_kind="text",
        summary="Cookie: sid=supersecret; pref=alsosensitive",
        artifacts=[
            b"api_key=sk-bytes12345678",
            {"raw": _StringySecret()},
            {"headers": "Cookie: auth=secret-one; tracking=secret-two"},
        ],
    )

    payload_text = json.dumps(event["payload"], sort_keys=True)
    assert "supersecret" in payload_text
    assert "alsosensitive" in payload_text
    assert "sk-bytes12345678" in payload_text
    assert "sk-object12345678" in payload_text
    assert "secret-one" in payload_text
    assert "secret-two" in payload_text
    assert event["payload"]["summary"] == "Cookie: sid=supersecret; pref=alsosensitive"


def test_translator_tool_result_preserves_local_structured_header_values() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_result(
        tool_use_id="tool-1",
        result_kind="json",
        summary="headers captured",
        artifacts=[
            {
                "headers": {
                    "X-Api-Key": "plain-secret-value",
                    "Private-Key": "plain-private-value",
                },
                "privateKey": "camel-private-value",
            }
        ],
    )

    assert event["payload"]["artifacts"] == [
        {
            "headers": {
                "X-Api-Key": "plain-secret-value",
                "Private-Key": "plain-private-value",
            },
            "privateKey": "camel-private-value",
        }
    ]


def test_translator_tool_result_preserves_local_json_like_x_api_key_strings() -> None:
    from iac_code.web.events import WebEventTranslator

    translator = WebEventTranslator("session-1")

    event = translator.tool_result(
        tool_use_id="tool-1",
        result_kind="text",
        summary='{"X-Api-Key":"plain-secret-value", "Private-Key":"plain-private-value"}',
        artifacts=['{"x_api_key":"another-secret-value"}'],
    )

    payload_text = json.dumps(event["payload"], sort_keys=True)
    assert "plain-secret-value" in payload_text
    assert "plain-private-value" in payload_text
    assert "another-secret-value" in payload_text


def test_publishing_translator_output_assigns_buffer_sequence(tmp_path) -> None:
    from iac_code.web.events import WebEventTranslator
    from iac_code.web.session_manager import WebSessionManager

    async def publish_translated_event() -> tuple[dict[str, object], dict[str, object]]:
        manager = WebSessionManager(projects_dir=tmp_path / "projects")
        session = manager.create_session(session_id="session-1")
        translated = WebEventTranslator("session-1").tool_finished(
            tool_use_id="tool-1",
            status="success",
            elapsed_ms=25,
            summary="done",
        )

        published = await session.events.publish(translated["type"], translated["payload"])
        return translated, published

    translated_event, published_event = asyncio.run(publish_translated_event())

    assert translated_event["sequence"] == 0
    assert published_event["type"] == translated_event["type"]
    assert published_event["payload"] == translated_event["payload"]
    assert published_event["sequence"] > 0


def test_frontend_reducer_applies_delta_nested_tools_and_tombstone(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "assistant.message.start",
              sequence: 1,
              payload: { messageId: "m1" },
            });
            state = reduceEvent(state, {
              type: "assistant.text.delta",
              sequence: 2,
              payload: { messageId: "m1", delta: "hello" },
            });
            state = reduceEvent(state, {
              type: "tool.started",
              sequence: 3,
              payload: { toolUseId: "parent", toolName: "read", status: "running" },
            });
            state = reduceEvent(state, {
              type: "tool.started",
              sequence: 4,
              payload: {
                toolUseId: "child",
                toolName: "parse",
                parentToolUseId: "parent",
                status: "running",
              },
            });
            state = reduceEvent(state, {
              type: "tool.result",
              sequence: 5,
              payload: { toolUseId: "child", resultKind: "text", summary: "parsed" },
            });
            const beforeTombstone = {
              text: state.messages.m1.text,
              children: state.tools.parent.children,
              childResults: state.tools.child.results.map((result) => result.summary),
              lastSequence: state.lastSequence,
            };
            state = reduceEvent(state, {
              type: "assistant.message.tombstone",
              sequence: 6,
              payload: { messageId: "m1", affectedToolUseIds: ["parent", "child"] },
            });
            console.log(JSON.stringify({
              beforeTombstone,
              hasMessage: Object.hasOwn(state.messages, "m1"),
              toolIds: Object.keys(state.tools),
              lastSequence: state.lastSequence,
            }));
            """
        ),
    )

    assert output == {
        "beforeTombstone": {
            "text": "hello",
            "children": ["child"],
            "childResults": ["parsed"],
            "lastSequence": 5,
        },
        "hasMessage": False,
        "toolIds": [],
        "lastSequence": 6,
    }


def test_frontend_reducer_finishes_tools_when_result_arrives(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "ok", toolName: "bash", status: "running" },
            });
            state = reduceEvent(state, {
              type: "tool.finished",
              sequence: 2,
              payload: { toolUseId: "ok", status: "input_complete" },
            });
            state = reduceEvent(state, {
              type: "tool.started",
              sequence: 3,
              payload: { toolUseId: "bad", toolName: "bash", status: "running" },
            });
            state = reduceEvent(state, {
              type: "tool.finished",
              sequence: 4,
              payload: { toolUseId: "bad", status: "input_complete" },
            });
            state = reduceEvent(state, {
              type: "tool.result",
              sequence: 5,
              payload: { toolUseId: "ok", resultKind: "text", summary: "done" },
            });
            state = reduceEvent(state, {
              type: "tool.result",
              sequence: 6,
              payload: { toolUseId: "bad", resultKind: "error", summary: "boom" },
            });

            console.log(JSON.stringify({ ok: state.tools.ok.status, bad: state.tools.bad.status }));
            """
        ),
    )

    assert output == {"ok": "completed", "bad": "failed"}


def test_frontend_reducer_applies_tool_progress_and_mcp_status_updates(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "tool.started",
              sequence: 1,
              payload: { toolUseId: "tool-1", toolName: "preview_stack", status: "running" },
            });
            state = reduceEvent(state, {
              type: "tool.progress",
              sequence: 2,
              payload: {
                toolUseId: "tool-1",
                publicName: "ROS PreviewStack",
                progress: 2,
                total: 5,
                message: "Validating",
              },
            });
            state = reduceEvent(state, {
              type: "mcp.status.updated",
              sequence: 3,
              payload: { servers: [{ name: "ros-server", status: "connected" }] },
            });

            console.log(JSON.stringify({
              tool: state.tools["tool-1"],
              mcpStatus: state.mcpStatus,
            }));
            """
        ),
    )

    assert output["tool"]["status"] == "running"
    assert output["tool"]["summary"] == "Validating"
    assert output["tool"]["progress"] == 2
    assert output["tool"]["total"] == 5
    assert output["tool"]["publicName"] == "ROS PreviewStack"
    assert output["mcpStatus"] == {"servers": [{"name": "ros-server", "status": "connected"}]}


def test_frontend_reducer_attaches_turn_tools_to_assistant_message(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "assistant.message.start",
              sequence: 1,
              payload: { turnId: "turn-1", messageId: "m1" },
            });
            state = reduceEvent(state, {
              type: "tool.started",
              sequence: 2,
              payload: { turnId: "turn-1", toolUseId: "tool-1", toolName: "fakeRosPlan" },
            });
            state = reduceEvent(state, {
              type: "local.shell.start",
              sequence: 3,
              payload: { turnId: "turn-1", toolUseId: "shell-1", command: "echo hi" },
            });

            console.log(JSON.stringify({
              toolUseIds: state.messages.m1.toolUseIds,
              toolMessageId: state.tools["tool-1"].messageId,
              shellMessageId: state.localShell["shell-1"].messageId,
            }));
            """
        ),
    )

    assert output == {
        "toolUseIds": ["tool-1", "shell-1"],
        "toolMessageId": "m1",
        "shellMessageId": "m1",
    }


def test_frontend_reducer_ignores_resync_zero_and_stale_sequences(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            let state = reduceEvent({}, {
              type: "assistant.message.start",
              sequence: 7,
              payload: { messageId: "m1" },
            });
            state = reduceEvent(state, {
              type: "session.resync.required",
              sequence: 0,
              payload: { afterSequence: 7, floorSequence: 10 },
            });
            const afterResync = state.lastSequence;
            state = reduceEvent(state, {
              type: "assistant.text.delta",
              sequence: 6,
              payload: { messageId: "m1", delta: "stale" },
            });
            const afterStale = state.lastSequence;
            state = reduceEvent(state, {
              type: "assistant.text.delta",
              sequence: 8,
              payload: { messageId: "m1", delta: "fresh" },
            });

            console.log(JSON.stringify({
              afterResync,
              afterStale,
              lastSequence: state.lastSequence,
              text: state.messages.m1.text,
            }));
            """
        ),
    )

    assert output == {
        "afterResync": 7,
        "afterStale": 7,
        "lastSequence": 8,
        "text": "fresh",
    }


def test_frontend_reducer_hydrates_legacy_tool_shape_before_appending_result(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { reduceEvent } from __EVENTS_MODULE__;

            const state = reduceEvent({
              tools: {
                legacy: { toolUseId: "legacy" },
              },
            }, {
              type: "tool.result",
              sequence: 1,
              payload: {
                toolUseId: "legacy",
                resultKind: "text",
                summary: "ok",
                artifacts: ["artifact"],
              },
            });

            console.log(JSON.stringify(state.tools.legacy));
            """
        ),
    )

    assert output == {
        "toolUseId": "legacy",
        "status": "completed",
        "input": "",
        "children": [],
        "results": [
            {
                "toolUseId": "legacy",
                "resultKind": "text",
                "summary": "ok",
                "artifacts": ["artifact"],
            }
        ],
        "artifacts": ["artifact"],
        "resultKind": "text",
        "summary": "ok",
    }


_ORPHANED_TOOL_HARNESS = """
import { renderToolCards } from __TOOL_CARDS_MODULE__;

class Element {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.textContent = "";
    this.className = "";
  }
  append(...children) {
    this.children.push(...children);
  }
}

function collect(node, result = { text: [], classNames: [] }) {
  if (node.textContent) {
    result.text.push(node.textContent);
  }
  if (node.className) {
    result.classNames.push(node.className);
  }
  for (const child of node.children || []) {
    collect(child, result);
  }
  return result;
}

globalThis.document = {
  createElement(tagName) {
    return new Element(tagName);
  },
};
"""


def test_frontend_tool_card_pending_tool_shows_canceled_when_turn_inactive(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": {
                  toolUseId: "shell-1",
                  command: "sleep 5",
                  status: "pending",
                  local: true,
                },
              },
            };

            console.log(JSON.stringify(collect(renderToolCards(state, { turnActive: false }))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_classes = " ".join(output["classNames"])
    assert "Canceled sleep 5" in rendered_text
    assert "Running sleep 5" not in rendered_text
    assert "is-active" not in rendered_classes


def test_frontend_tool_card_pending_tool_stays_running_when_turn_active(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": {
                  toolUseId: "shell-1",
                  command: "sleep 5",
                  status: "pending",
                  local: true,
                },
              },
            };

            console.log(JSON.stringify(collect(renderToolCards(state, { turnActive: true }))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_classes = " ".join(output["classNames"])
    assert "Running sleep 5" in rendered_text
    assert "Canceled sleep 5" not in rendered_text
    assert "is-active" in rendered_classes


def test_frontend_tool_group_finalizes_orphaned_tools_when_turn_inactive(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": {
                  toolUseId: "shell-1",
                  command: "ls",
                  status: "completed",
                  exitCode: 0,
                  local: true,
                },
                "shell-2": {
                  toolUseId: "shell-2",
                  command: "sleep 5",
                  status: "pending",
                  local: true,
                },
              },
            };

            console.log(JSON.stringify(collect(renderToolCards(state, { grouped: true, turnActive: false }))));
            """
        ),
    )

    rendered_text = " ".join(output["text"])
    rendered_classes = " ".join(output["classNames"])
    assert "Canceled sleep 5" in rendered_text
    assert "Running" not in rendered_text
    assert "is-active" not in rendered_classes


def test_frontend_tool_group_opens_during_active_turn_with_cards_collapsed(tmp_path) -> None:
    # 运行中(turnActive):工具组默认展开——让用户看到正在执行的工具列表;组内每张卡仍收起。
    # 关键回归:即便传入 openToolUseId 指向组内一张*已完成*的尾部卡,turnActive 下它也必须保持
    # 收起——renderToolGroup 须把 turnActive 透传给组内 renderToolCard,否则该卡走默认分支
    # (返回 Boolean(isLatest))被展开,正是「运行中组里最后一个工具仍展开」的成因。
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": { toolUseId: "shell-1", command: "ls", status: "completed", exitCode: 0, local: true },
                "shell-2": { toolUseId: "shell-2", command: "pwd", status: "completed", exitCode: 0, local: true },
              },
            };

            function scan(node, out) {
              const tokens = typeof node.className === "string" ? node.className.split(" ") : [];
              if (tokens.includes("tool-group")) {
                out.group = node.open === true;
              }
              if (tokens.includes("tool-card")) {
                out.cards.push(node.open === true);
              }
              for (const child of node.children || []) scan(child, out);
              return out;
            }

            // openToolUseId 指向尾部已完成卡 shell-2:回合进行中它仍须收起。
            const rendered = renderToolCards(state, { grouped: true, turnActive: true, openToolUseId: "shell-2" });
            console.log(JSON.stringify(scan(rendered, { group: null, cards: [] })));
            """
        ),
    )

    assert output["group"] is True  # 组默认展开
    assert output["cards"] == [False, False]  # 组内卡片(含 openToolUseId 命中的尾部卡)保持收起


def test_frontend_tool_group_opens_latest_card_when_turn_resting(tmp_path) -> None:
    # 静息态(turnActive=false)对照:openToolUseId 命中的尾部已完成卡应当展开(转录尾部最新卡可见),
    # 组默认展开。确保上面的收起只由 turnActive 触发,而非误伤静息态的既有行为。
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": { toolUseId: "shell-1", command: "ls", status: "completed", exitCode: 0, local: true },
                "shell-2": { toolUseId: "shell-2", command: "pwd", status: "completed", exitCode: 0, local: true },
              },
            };

            function scan(node, out) {
              const tokens = typeof node.className === "string" ? node.className.split(" ") : [];
              if (tokens.includes("tool-group")) out.group = node.open === true;
              if (tokens.includes("tool-card")) out.cards.push(node.open === true);
              for (const child of node.children || []) scan(child, out);
              return out;
            }

            const rendered = renderToolCards(state, { grouped: true, turnActive: false, openToolUseId: "shell-2" });
            console.log(JSON.stringify(scan(rendered, { group: null, cards: [] })));
            """
        ),
    )

    assert output["group"] is True  # holdsLatest → 组展开
    assert output["cards"] == [False, True]  # 尾部最新卡展开,其余收起


def test_frontend_tool_group_stays_collapsed_in_resting_pipeline_transcript(tmp_path) -> None:
    # 静息的流水线转录(collapseNonComplete=true, turnActive=false):整组保持收起,避免 reload 后
    # 一屏铺开(近期设计)。仅运行中才自动展开工具组。
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": { toolUseId: "shell-1", command: "ls", status: "completed", exitCode: 0, local: true },
                "shell-2": { toolUseId: "shell-2", command: "pwd", status: "completed", exitCode: 0, local: true },
              },
            };

            function scan(node, out) {
              const tokens = typeof node.className === "string" ? node.className.split(" ") : [];
              if (tokens.includes("tool-group")) out.group = node.open === true;
              for (const child of node.children || []) scan(child, out);
              return out;
            }

            const rendered = renderToolCards(state, { grouped: true, turnActive: false, collapseNonComplete: true });
            console.log(JSON.stringify(scan(rendered, { group: null })));
            """
        ),
    )

    assert output["group"] is False


def test_frontend_tool_group_auto_collapses_when_all_done_and_turn_moved_on(tmp_path) -> None:
    # Issue #1:组内所有工具都跑完、且助手已产出正文(下一事件非工具相关)时,工具组应自动收起。
    # 这体现在 latestToolUseIdForTranscript 返回空串(openToolUseId=""),使 holdsLatest 转假;
    # 即便 turnActive=true,组也不再强制展开——groupActive(无进行中工具)与 holdsLatest 皆为假 → 收起。
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": { toolUseId: "shell-1", command: "ls", status: "completed", exitCode: 0, local: true },
                "shell-2": { toolUseId: "shell-2", command: "pwd", status: "completed", exitCode: 0, local: true },
              },
            };

            function scan(node, out) {
              const tokens = typeof node.className === "string" ? node.className.split(" ") : [];
              if (tokens.includes("tool-group")) out.group = node.open === true;
              for (const child of node.children || []) scan(child, out);
              return out;
            }

            // 助手已开始作答 → 转录尾部最新工具 id 为空。回合仍进行中(turnActive)但组无进行中工具。
            const rendered = renderToolCards(state, { grouped: true, turnActive: true, openToolUseId: "" });
            console.log(JSON.stringify(scan(rendered, { group: null })));
            """
        ),
    )

    assert output["group"] is False  # 所有工具完成 + 已作答 → 工具组自动收起


def test_frontend_tool_group_stays_open_during_active_turn_while_tool_in_progress(tmp_path) -> None:
    # 保底:回合进行中且组内有工具*正在执行*(groupActive)时,即使 openToolUseId 为空,工具组仍展开
    # ——保留原始需求「运行中工具组要展开」,确认自动收起只发生在工具全部完成之后。
    output = _run_reducer_script(
        tmp_path,
        _ORPHANED_TOOL_HARNESS
        + textwrap.dedent(
            """
            const state = {
              localShell: {
                "shell-1": { toolUseId: "shell-1", command: "ls", status: "completed", exitCode: 0, local: true },
                "shell-2": { toolUseId: "shell-2", command: "pwd", status: "running", local: true },
              },
            };

            function scan(node, out) {
              const tokens = typeof node.className === "string" ? node.className.split(" ") : [];
              if (tokens.includes("tool-group")) out.group = node.open === true;
              for (const child of node.children || []) scan(child, out);
              return out;
            }

            const rendered = renderToolCards(state, { grouped: true, turnActive: true, openToolUseId: "" });
            console.log(JSON.stringify(scan(rendered, { group: null })));
            """
        ),
    )

    assert output["group"] is True  # 组内有进行中工具 → 保持展开


_PERMISSION_RICH_HARNESS = """
import { renderPermissionRequest, nextPermissionSelection } from __BLOCKING_MODULE__;

class Element {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.type = "";
    this._listeners = {};
  }
  append(...children) {
    for (const child of children) {
      if (child !== null && child !== undefined && child !== "") {
        this.children.push(child);
      }
    }
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  click() {
    for (const fn of this._listeners.click || []) {
      fn({ preventDefault() {}, stopPropagation() {} });
    }
  }
  setAttribute(name, value) {
    this[name] = value;
  }
}

globalThis.document = {
  createElement(tagName) {
    return new Element(tagName);
  },
};

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) {
    walk(child, visit);
  }
}

function findAll(node, predicate) {
  const out = [];
  walk(node, (n) => {
    if (predicate(n)) {
      out.push(n);
    }
  });
  return out;
}
"""


def test_frontend_permission_renders_numbered_options_with_first_selected(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _PERMISSION_RICH_HARNESS
        + textwrap.dedent(
            """
            const request = {
              requestId: "req-1",
              payload: {
                sessionId: "s1",
                toolName: "bash",
                message: "需要临时允许网络访问来测试 curl www.baidu.com 是否能正常连通。",
                command: "curl www.baidu.com",
                choices: [
                  { id: "allow_once", label: "仅本次允许" },
                  { id: "always_allow", label: "本会话始终允许 curl:*" },
                  { id: "reject_once", label: "仅本次拒绝" },
                  { id: "always_deny", label: "本会话始终拒绝 curl:*" },
                ],
              },
            };

            const panel = renderPermissionRequest(request, {});
            const rows = findAll(panel, (n) => (n.className || "").includes("blocking-option-row"));
            const indices = findAll(panel, (n) => (n.className || "").includes("blocking-option-index")).map(
              (n) => n.textContent,
            );
            const labels = findAll(panel, (n) => (n.className || "").includes("blocking-option-label")).map(
              (n) => n.textContent,
            );
            const selected = rows.filter((r) => (r.className || "").includes("is-selected"));
            const submit = findAll(panel, (n) => (n.className || "").includes("blocking-submit"));
            const allText = findAll(panel, () => true).map((n) => n.textContent).join(" ");

            console.log(JSON.stringify({
              rowCount: rows.length,
              indices,
              labels,
              selectedCount: selected.length,
              selectedIndex: selected.length === 1 ? selected[0].dataset.index : null,
              submitCount: submit.length,
              hasSkip: allText.includes("跳过"),
              hasMessage: allText.includes("需要临时允许网络访问"),
              hasCommand: allText.includes("curl www.baidu.com"),
            }));
            """
        ),
    )

    assert output["rowCount"] == 4
    assert output["indices"] == ["1", "2", "3", "4"]
    assert output["labels"] == [
        "仅本次允许",
        "本会话始终允许 curl:*",
        "仅本次拒绝",
        "本会话始终拒绝 curl:*",
    ]
    assert output["selectedCount"] == 1
    assert output["selectedIndex"] == "0"
    assert output["submitCount"] == 1
    assert output["hasSkip"] is False
    assert output["hasMessage"] is True
    assert output["hasCommand"] is True


def test_frontend_permission_row_click_answers_with_choice_id(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _PERMISSION_RICH_HARNESS
        + textwrap.dedent(
            """
            const answers = [];
            const request = {
              requestId: "req-9",
              payload: {
                sessionId: "sess-9",
                toolName: "bash",
                message: "允许 Bash?",
                command: "sleep 5",
                choices: [
                  { id: "allow_once", label: "仅本次允许" },
                  { id: "reject_once", label: "仅本次拒绝" },
                ],
              },
            };

            const panel = renderPermissionRequest(request, {
              onPermissionAnswer: (requestId, answer) => {
                answers.push({ requestId, answer });
              },
            });

            const rows = findAll(panel, (n) => (n.className || "").includes("blocking-option-row"));
            rows[1].click();

            console.log(JSON.stringify(answers));
            """
        ),
    )

    assert output == [
        {
            "requestId": "req-9",
            "answer": {"sessionId": "sess-9", "choice": "reject_once"},
        }
    ]


def test_frontend_permission_submit_button_answers_selected_choice(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _PERMISSION_RICH_HARNESS
        + textwrap.dedent(
            """
            const answers = [];
            const request = {
              requestId: "req-s",
              payload: {
                sessionId: "sess-s",
                toolName: "bash",
                message: "允许 Bash?",
                command: "sleep 5",
                choices: [
                  { id: "allow_once", label: "仅本次允许" },
                  { id: "reject_once", label: "仅本次拒绝" },
                ],
              },
            };

            const panel = renderPermissionRequest(request, {
              onPermissionAnswer: (requestId, answer) => {
                answers.push({ requestId, answer });
              },
            });

            const submit = findAll(panel, (n) => (n.className || "").includes("blocking-submit"))[0];
            submit.click();

            console.log(JSON.stringify(answers));
            """
        ),
    )

    assert output == [
        {
            "requestId": "req-s",
            "answer": {"sessionId": "sess-s", "choice": "allow_once"},
        }
    ]


def test_frontend_permission_next_selection_clamps_within_bounds(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _PERMISSION_RICH_HARNESS
        + textwrap.dedent(
            """
            const count = 4;
            console.log(JSON.stringify({
              downFromStart: nextPermissionSelection(0, "ArrowDown", count),
              downJ: nextPermissionSelection(0, "j", count),
              downFromLast: nextPermissionSelection(3, "ArrowDown", count),
              upFromMiddle: nextPermissionSelection(2, "ArrowUp", count),
              upK: nextPermissionSelection(2, "k", count),
              upFromStart: nextPermissionSelection(0, "ArrowUp", count),
              other: nextPermissionSelection(1, "x", count),
            }));
            """
        ),
    )

    assert output == {
        "downFromStart": 1,
        "downJ": 1,
        "downFromLast": 3,
        "upFromMiddle": 1,
        "upK": 1,
        "upFromStart": 0,
        "other": 1,
    }


def test_frontend_tool_command_text_reflects_failure(tmp_path) -> None:
    # Issue 4: a failed tool must not read as "已完成/已运行". The done-phrase's
    # leading "已…" turns into "…失败" so the title shows the action actually failed,
    # while canceled/denied keep their own wording and success is unchanged.
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { toolCommandText, isToolFailed } from __TOOL_CARDS_MODULE__;
            console.log(JSON.stringify({
              completeFailed: toolCommandText({ toolName: "complete_step", status: "failed" }),
              completeOk: toolCommandText({ toolName: "complete_step", status: "completed" }),
              readFailed: toolCommandText({ toolName: "read_file", status: "failed" }),
              failedByStatus: isToolFailed({ status: "failed" }),
              canceledNotFailed: isToolFailed({ status: "canceled" }),
              deniedNotFailed: isToolFailed({ status: "denied" }),
              okNotFailed: isToolFailed({ status: "completed" }),
            }));
            """
        ),
    )
    assert output["completeFailed"] == "Step failed"
    assert output["completeOk"] == "Completed step"
    # 通用工具的过去式短语开头也是"已…"，失败时同样转成"…失败"。
    assert "Read failed" in output["readFailed"]
    assert output["failedByStatus"] is True
    assert output["canceledNotFailed"] is False
    assert output["deniedNotFailed"] is False
    assert output["okNotFailed"] is False


def test_frontend_tool_cards_keep_latest_card_open(tmp_path) -> None:
    # Issue 3: the newest tool card in the transcript tail stays expanded until the
    # next message/tool arrives, so a fast start→finish tool no longer just "flashes".
    # Passing openToolUseId marks exactly that card open; earlier completed cards fold.
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderToolCards } from __TOOL_CARDS_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
                this.open = false;
              }
              append(...children) { this.children.push(...children); }
            }
            globalThis.document = { createElement: (t) => new Element(t) };

            function cards(node, out = []) {
              if (node.tagName === "DETAILS" && node.dataset?.toolUseId) {
                out.push({ id: node.dataset.toolUseId, open: node.open === true });
              }
              for (const child of node.children || []) cards(child, out);
              return out;
            }

            const state = {
              tools: {
                t1: { toolUseId: "t1", toolName: "read_file", status: "completed", results: [{ output: "a" }] },
                t2: { toolUseId: "t2", toolName: "read_file", status: "completed", results: [{ output: "b" }] },
              },
            };
            const latest = cards(renderToolCards(state, { openToolUseId: "t2" }));
            const none = cards(renderToolCards(state, {}));
            console.log(JSON.stringify({ latest, none }));
            """
        ),
    )
    latest = {card["id"]: card["open"] for card in output["latest"]}
    assert latest == {"t1": False, "t2": True}
    # Without an openToolUseId hint, no completed card is force-opened.
    none = {card["id"]: card["open"] for card in output["none"]}
    assert none == {"t1": False, "t2": False}


def test_pipeline_step_diagrams_render_buttons(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            const diagrams = [
              { candidateName: "方案A", diagramId: "d1", candidateIndex: 0, format: "mermaid" },
              { candidateName: "方案B", diagramId: "d2", candidateIndex: 1, format: "mermaid" },
              // 部署步骤按真实路径写出的最终模板:无 candidateIndex,须被候选选择器过滤掉(不产生重复按钮)。
              { candidateName: null, diagramId: "d3", sourceRelPath: "/tmp/final.yml",
                candidateIndex: null, format: "mermaid" },
            ];
            const toggled = [];
            const selected = [];
            // toggleDiagram 返回切换后的开启态:首点 d2 返回 true(打开)。
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: (item) => { toggled.push(item); return true; },
              onSelectCandidate: (item) => { selected.push(item); },
            });
            // 新契约:按钮组只返回不挂载(调用方在提示文字之后再 append 到 body 末尾)。
            const inBodyBeforeMount = collectByClass(group.body, "pipeline-step-diagram-link").length;
            const links = collectByClass(group.diagramGroup, "pipeline-step-diagram-link");
            const selects = collectByClass(group.diagramGroup, "pipeline-step-select-button");
            links[1].__click();
            console.log(JSON.stringify({
              linkCount: links.length,
              texts: links.map((b) => b.textContent),
              toggledId: toggled.length === 1 ? toggled[0].diagramId : null,
              linkOpenClass: links[1].className,
              selectCount: selects.length,
              inBodyBeforeMount,
              groupClass: group.diagramGroup ? group.diagramGroup.className : null,
            }));
            """
        ),
    )

    # 传入 3 张图,其中一张无 candidateIndex(部署产物)→ 候选选择器过滤后只剩 2 张候选。
    assert output["linkCount"] == 2
    assert any("方案A" in txt for txt in output["texts"])
    assert any("方案B" in txt for txt in output["texts"])
    assert not any("final.yml" in txt for txt in output["texts"])
    assert output["toggledId"] == "d2"
    # 切换返回 true → 链接标记 is-open(纯装饰,不影响切换语义)。
    assert "is-open" in output["linkOpenClass"]
    # awaitingInput(status=="input") + onSelectCandidate → 每候选一枚「选择该方案」按钮。
    assert output["selectCount"] == 2
    # 契约:构造时链接不在 body 内(延后挂载在提示文字之后),而是随 group.diagramGroup 返回。
    assert output["inBodyBeforeMount"] == 0
    assert output["groupClass"] == "pipeline-step-diagrams"


def test_pipeline_step_renders_all_authoritative_candidates_even_without_diagram(tmp_path) -> None:
    # 根因修复:选择器按权威候选表(input_required.options)渲染,而非「架构图能否渲染」。
    # 出了 2 个方案(idx0/idx1),但只有 idx1 的模板能转 mermaid(idx0 模板损坏无图)。
    # 期望:仍渲出 2 行、2 枚「选择该方案」按钮;idx1 行有「查看架构图」链接,idx0 行无链接
    # (改用纯文本名标签占位)但照样可选。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this.attrs = {};
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute(k, v) { this.attrs[k] = v; }
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.split(" ").includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            // 权威候选表 2 项;架构图只有 idx1(idx0 模板损坏,diagram_items 已丢弃)。
            const candidates = [
              { candidateName: "经济极简方案", candidateIndex: 0, summary: "s0" },
              { candidateName: "均衡性价比方案", candidateIndex: 1, summary: "s1" },
            ];
            const diagrams = [
              { candidateName: "均衡性价比方案", diagramId: "d2", candidateIndex: 1, format: "mermaid" },
            ];
            const selected = [];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              candidates,
              toggleDiagram: () => true,
              // 模拟真实调用方:只取候选名 + 序号回传(见 app.js handleSelectPipelineCandidate)。
              onSelectCandidate: (item) => {
                selected.push({ candidateName: item.candidateName, candidateIndex: item.candidateIndex });
              },
            });
            const rows = collectByClass(group.diagramGroup, "pipeline-step-diagram-item");
            const links = collectByClass(group.diagramGroup, "pipeline-step-diagram-link");
            const names = collectByClass(group.diagramGroup, "pipeline-step-diagram-name");
            const selects = collectByClass(group.diagramGroup, "pipeline-step-select-button");
            // 点第一枚「选择该方案」→ 应回传 idx0(权威候选,原本因无图而消失的那个)。
            selects[0].__click();
            selects[0].__click();
            console.log(JSON.stringify({
              rowCount: rows.length,
              linkCount: links.length,
              linkText: links.map((l) => l.textContent),
              nameCount: names.length,
              nameText: names.map((n) => n.textContent),
              selectCount: selects.length,
              firstSelected: selected.length ? selected[0] : null,
            }));
            """
        ),
    )
    # 2 个方案都成行、都可选(修复前只有 1 行/1 按钮)。
    assert output["rowCount"] == 2
    assert output["selectCount"] == 2
    # 只有 idx1 有「查看架构图」链接;idx0 无图 → 纯文本名标签占位。
    assert output["linkCount"] == 1
    assert any("均衡性价比方案" in txt for txt in output["linkText"])
    assert output["nameCount"] == 1
    assert output["nameText"] == ["经济极简方案"]
    # 先前消失的 idx0 现在可选,回传其权威 name/index。
    assert output["firstSelected"] == {"candidateName": "经济极简方案", "candidateIndex": 0}


def test_pipeline_step_diagram_marks_selected_candidate_with_check(tmp_path) -> None:
    # 选定方案后:该候选行加 is-selected 且追加一枚绿色对勾(pipeline-step-diagram-check),
    # 未选中的候选不加。此时步骤已离开 "input"→无「选择该方案」按钮,仅留对勾标出所选。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this.attrs = {};
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute(k, v) { this.attrs[k] = v; }
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.split(" ").includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            // 步骤已完成(status 非 "input"):无「选择该方案」按钮。selectedCandidate 指向序号 1。
            const step = { stepId: "confirm_and_select", status: "step_completed" };
            const message = { kind: "pipeline_step", pipelineStep: step };
            const diagrams = [
              { candidateName: "标准高可用方案", diagramId: "d1", candidateIndex: 0, format: "mermaid" },
              { candidateName: "增强高可用方案", diagramId: "d2", candidateIndex: 1, format: "mermaid" },
            ];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: () => true,
              onSelectCandidate: () => {},
              selectedCandidate: { candidateIndex: 1, candidateName: "增强高可用方案" },
            });
            const rows = collectByClass(group.diagramGroup, "pipeline-step-diagram-item");
            const checks = collectByClass(group.diagramGroup, "pipeline-step-diagram-check");
            const selectedRows = collectByClass(group.diagramGroup, "is-selected");
            const selectBtns = collectByClass(group.diagramGroup, "pipeline-step-select-button");
            console.log(JSON.stringify({
              rowCount: rows.length,
              checkCount: checks.length,
              checkText: checks.map((c) => c.textContent),
              checkAria: checks.map((c) => c.attrs["aria-label"]),
              selectedRowCount: selectedRows.length,
              // 被标 is-selected 的行,其链接文案应是选中的候选名。
              selectedRowLink: selectedRows.length
                ? collectByClass(selectedRows[0], "pipeline-step-diagram-link")[0].textContent
                : null,
              selectBtnCount: selectBtns.length,
            }));
            """
        ),
    )
    assert output["rowCount"] == 2
    # 仅选中的一行有对勾。
    assert output["checkCount"] == 1
    assert output["checkText"] == ["✓"]
    assert output["checkAria"] == ["Selected"]
    assert output["selectedRowCount"] == 1
    assert "增强高可用方案" in output["selectedRowLink"]
    # 步骤已完成 → 没有「选择该方案」按钮,只剩对勾。
    assert output["selectBtnCount"] == 0


def test_pipeline_step_select_button_suppressed_when_already_selected(tmp_path) -> None:
    # Issue 3:即便步骤帧仍是 "input"(awaitingInput=true),只要已解析出 selectedCandidate,
    # 就绝不再整排渲染「选择该方案」——仅留对勾。防止确认后按钮全部复位成可选。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this.attrs = {};
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute(k, v) { this.attrs[k] = v; }
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.split(" ").includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            // status 仍是 "input",但已选定序号 0 → 按钮被门控抑制,只剩对勾。
            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            const diagrams = [
              { candidateName: "最低成本测试方案", diagramId: "d1", candidateIndex: 0, format: "mermaid" },
              { candidateName: "高可用方案", diagramId: "d2", candidateIndex: 1, format: "mermaid" },
            ];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: () => true,
              onSelectCandidate: () => {},
              selectedCandidate: { candidateIndex: 0, candidateName: "最低成本测试方案" },
            });
            const checks = collectByClass(group.diagramGroup, "pipeline-step-diagram-check");
            const selectBtns = collectByClass(group.diagramGroup, "pipeline-step-select-button");
            console.log(JSON.stringify({
              checkCount: checks.length,
              selectBtnCount: selectBtns.length,
            }));
            """
        ),
    )
    # 已选 → 无「选择该方案」按钮(整排抑制),仅选中行一枚对勾。
    assert output["selectBtnCount"] == 0
    assert output["checkCount"] == 1


def test_pipeline_step_select_button_two_click_confirm(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this.disabled = false;
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            const diagrams = [
              { candidateName: "方案A", diagramId: "d1", candidateIndex: 0, format: "mermaid" },
            ];
            const selected = [];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: () => true,
              onSelectCandidate: (item) => { selected.push(item); },
            });
            const btn = collectByClass(group.diagramGroup, "pipeline-step-select-button")[0];
            const initial = { text: btn.textContent, cls: btn.className };
            btn.__click(); // 首击:进入确认态,尚未提交。
            const afterFirst = { text: btn.textContent, cls: btn.className, selected: selected.length };
            btn.__click(); // 再击:确认提交。
            const afterSecond = { text: btn.textContent, cls: btn.className, selected: selected.length,
                                  selName: selected[0]?.candidateName, selIndex: selected[0]?.candidateIndex,
                                  disabled: btn.disabled };
            console.log(JSON.stringify({ initial, afterFirst, afterSecond }));
            """
        ),
    )

    # 初始:朴素按钮,未确认。
    assert output["initial"]["text"] == "Select this option"
    assert "is-confirming" not in output["initial"]["cls"]
    # 首击:进入「确认选择?」高亮态,不提交。
    assert output["afterFirst"]["text"] == "Confirm selection?"
    assert "is-confirming" in output["afterFirst"]["cls"]
    assert output["afterFirst"]["selected"] == 0
    # 再击:提交(带 candidateName/candidateIndex),进入提交中禁用态。
    assert output["afterSecond"]["selected"] == 1
    assert output["afterSecond"]["selName"] == "方案A"
    assert output["afterSecond"]["selIndex"] == 0
    assert "is-submitting" in output["afterSecond"]["cls"]
    assert output["afterSecond"]["text"] == "Selecting…"
    assert output["afterSecond"]["disabled"] is True


def test_pipeline_step_select_buttons_locked_while_one_submitting(tmp_path) -> None:
    # Issue 2:一枚候选进入提交态后,其它候选按钮必须整体锁定,点击无效(此前只锁单个按钮)。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this.disabled = false;
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            const diagrams = [
              { candidateName: "方案A", diagramId: "d1", candidateIndex: 0, format: "mermaid" },
              { candidateName: "方案B", diagramId: "d2", candidateIndex: 1, format: "mermaid" },
            ];
            const selected = [];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: () => true,
              // 提交回调返回永不 resolve 的 promise,模拟「续跑流水线中」——提交锁不解开。
              onSelectCandidate: (item) => { selected.push(item); return new Promise(() => {}); },
            });
            const btns = collectByClass(group.diagramGroup, "pipeline-step-select-button");
            const btnA = btns[0];
            const btnB = btns[1];
            btnA.__click(); // A 首击:武装。
            btnA.__click(); // A 再击:确认提交 → 全局锁。
            // B 现在被锁:两击都应无效(既不武装,也不提交)。
            btnB.__click();
            btnB.__click();
            console.log(JSON.stringify({
              selectedCount: selected.length,
              selectedName: selected[0]?.candidateName || null,
              btnBText: btnB.textContent,
              btnBCls: btnB.className,
            }));
            """
        ),
    )

    # 只有 A 被提交一次;B 被锁,两击后仍是朴素按钮(未武装、未提交)。
    assert output["selectedCount"] == 1
    assert output["selectedName"] == "方案A"
    assert output["btnBText"] == "Select this option"
    assert "is-confirming" not in output["btnBCls"]
    assert "is-submitting" not in output["btnBCls"]


def test_regroup_pipeline_messages_makes_candidate_subtrees_contiguous(tmp_path) -> None:
    # Issue 3:并行候选事件按序号交错到达;渲染前须按 groupId/parentGroupId 把每棵候选子树
    # 重排成连续段,否则方案0的内容会被错挂到方案1(模板生成空、成本估算错位)。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            // app.js 模块顶层会 getElementById / addEventListener;导入前先给最小 document 桩。
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            const { regroupPipelineMessages } = await import(__APP_MODULE__);

            // 交错(序号)顺序:step, c0, c1, c0-tmpl, c1-tmpl, pl-c0-tmpl, pl-c1-tmpl, c0-cost, pl-c0-cost。
            const messages = [
              { messageId: "plmk-run", kind: "pipeline_step",
                pipelineStep: { groupId: "step:run", parentGroupId: null }, sequence: 1 },
              { messageId: "plmk-c0", kind: "pipeline_candidate",
                pipelineStep: { groupId: "candidate:c0", parentGroupId: "step:run" }, sequence: 2 },
              { messageId: "plmk-c1", kind: "pipeline_candidate",
                pipelineStep: { groupId: "candidate:c1", parentGroupId: "step:run" }, sequence: 3 },
              { messageId: "plmk-c0-tmpl", kind: "pipeline_sub_step",
                pipelineStep: { groupId: "c0:tmpl", parentGroupId: "candidate:c0" }, sequence: 4 },
              { messageId: "plmk-c1-tmpl", kind: "pipeline_sub_step",
                pipelineStep: { groupId: "c1:tmpl", parentGroupId: "candidate:c1" }, sequence: 5 },
              { messageId: "pl-c0-tmpl", role: "assistant", sequence: 6 },
              { messageId: "pl-c1-tmpl", role: "assistant", sequence: 7 },
              { messageId: "plmk-c0-cost", kind: "pipeline_sub_step",
                pipelineStep: { groupId: "c0:cost", parentGroupId: "candidate:c0" }, sequence: 8 },
              { messageId: "pl-c0-cost", role: "assistant", sequence: 9 },
            ];
            const ordered = regroupPipelineMessages(messages).map((m) => m.messageId);

            // 普通对话(无 pipelineStep)passthrough:顺序原样保留。
            const plain = [{ messageId: "m1", role: "user", sequence: 1 },
                           { messageId: "m2", role: "assistant", sequence: 2 }];
            const plainOrdered = regroupPipelineMessages(plain).map((m) => m.messageId);

            console.log(JSON.stringify({ ordered, plainOrdered, len: ordered.length }));
            """
        ),
    )

    # 候选0 整棵子树(标记+模板内容+成本标记+成本内容)先连续,再到候选1 子树。
    assert output["ordered"] == [
        "plmk-run",
        "plmk-c0",
        "plmk-c0-tmpl",
        "pl-c0-tmpl",
        "plmk-c0-cost",
        "pl-c0-cost",
        "plmk-c1",
        "plmk-c1-tmpl",
        "pl-c1-tmpl",
    ]
    assert output["len"] == 9  # 不丢任何消息。
    assert output["plainOrdered"] == ["m1", "m2"]  # 普通对话零影响。


def test_pipeline_step_select_button_absent_when_not_awaiting_input(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            const diagrams = [
              { candidateName: "方案A", diagramId: "d1", candidateIndex: 0, format: "mermaid" },
            ];
            // status 非 "input":选定后流水线推进,该步骤不再等待输入 → 选择按钮消失,链接仍在。
            const group = renderPipelineMarkerGroup(
              { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "completed" } },
              { diagrams, toggleDiagram: () => true, onSelectCandidate: () => {} });
            console.log(JSON.stringify({
              links: collectByClass(group.diagramGroup, "pipeline-step-diagram-link").length,
              selects: collectByClass(group.diagramGroup, "pipeline-step-select-button").length,
            }));
            """
        ),
    )

    # 已结束步骤:查看架构图链接保留,选择按钮消失(贴合「选择后按钮消失」)。
    assert output["links"] == 1
    assert output["selects"] == 0


def test_pipeline_step_diagrams_absent_for_other_steps(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);

            const g1 = renderPipelineMarkerGroup(
              { kind: "pipeline_step", pipelineStep: { stepId: "intent_parsing", status: "input" } },
              {
                diagrams: [{ candidateName: "X", diagramId: "d1", mermaidSource: "graph TD;a-->b", format: "mermaid" }],
                toggleDiagram: () => {},
              });
            const g2 = renderPipelineMarkerGroup(
              { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } },
              { diagrams: [], toggleDiagram: () => {} });
            console.log(JSON.stringify({
              otherStep: collectByClass(g1.diagramGroup, "pipeline-step-diagram-link").length,
              emptyDiagrams: collectByClass(g2.diagramGroup, "pipeline-step-diagram-link").length,
              otherStepGroup: g1.diagramGroup,
              emptyGroup: g2.diagramGroup,
            }));
            """
        ),
    )

    assert output["otherStep"] == 0
    assert output["emptyDiagrams"] == 0
    # 非选方案步骤 / 无图时不构建按钮组,diagramGroup 为 null。
    assert output["otherStepGroup"] is None
    assert output["emptyGroup"] is None


def test_pipeline_candidate_diagram_shows_costbearing_price_after_toggle(tmp_path) -> None:
    # 候选卡「架构图」展开后,询价块必须存活并携带来自 costItems 的价格。
    # RED 复现两个 Critical:#1 未 await renderMermaid → replaceChildren 抹掉 price;
    # #2 match 命中无价的 snapshot diagram → 渲「暂无询价信息」。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderPipelineWorkspace } from __PIPELINE_MODULE__;

            class Element {
              constructor(tagName) {
                this.tagName = String(tagName).toUpperCase();
                this.children = [];
                this.dataset = {};
                this.textContent = "";
                this.className = "";
                this.open = false;
                this._listeners = {};
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              addEventListener(type, handler) {
                (this._listeners[type] ||= []).push(handler);
              }
              dispatchEvent(evt) {
                const handlers = this._listeners[evt.type] || [];
                let ret;
                for (const handler of handlers) {
                  ret = handler(evt);
                }
                return ret;
              }
              set innerHTML(value) {
                this._innerHTML = value;
              }
              get innerHTML() {
                return this._innerHTML || "";
              }
            }

            globalThis.document = {
              head: new Element("head"),
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            // 预置 window.mermaid,使 loadMermaid 走 `if (window.mermaid) return resolve(...)`
            // 分支,永不注入 <script>;mermaid.render 同步返回 {svg}。
            globalThis.window = {
              mermaid: {
                initialize() {},
                render(id, src) {
                  return { svg: "<svg>ok</svg>" };
                },
              },
            };

            function findByClass(node, className, acc = []) {
              if (node && typeof node.className === "string" && node.className.split(" ").includes(className)) {
                acc.push(node);
              }
              for (const child of (node && node.children) || []) {
                findByClass(child, className, acc);
              }
              return acc;
            }

            function collectText(node, acc = []) {
              if (node && node.textContent) acc.push(node.textContent);
              for (const child of (node && node.children) || []) {
                collectText(child, acc);
              }
              return acc;
            }

            const state = {
              pipelineSnapshot: {
                contextId: "ctx-price",
                display: {
                  diagrams: [
                    // 无价的 snapshot 图(来自 DiagramEvent,无 diagramId / cost)。
                    { candidateName: "方案A", candidateIndex: 0, mermaidSource: "graph TD\\n A-->B" },
                  ],
                },
              },
              candidateDetails: [
                { candidateName: "方案A", candidateIndex: 0, summary: "经济方案" },
              ],
              // 带价的 webDiagram(来自后端 diagram_items,携带 diagramId + costItems)。
              webDiagrams: [
                {
                  diagramId: "0:tmpl.yaml",
                  candidateName: "方案A",
                  candidateIndex: 0,
                  mermaidSource: "graph TD\\n A-->B",
                  costItems: [{ name: "ECS", monthly_cost: "¥100" }],
                  totalMonthlyCost: "¥100/月",
                },
              ],
            };

            const rendered = renderPipelineWorkspace(state, {});
            const details = findByClass(rendered, "pipeline-candidate-diagram")[0];
            if (!details) {
              console.log(JSON.stringify({ error: "no diagram details element found" }));
            } else {
              details.open = true;
              await details.dispatchEvent({ type: "toggle" });
              await new Promise((r) => setTimeout(r, 0));
              const body = findByClass(details, "pipeline-candidate-diagram-body")[0];
              const priceValues = findByClass(body, "diagram-price-value");
              const priceEmpty = findByClass(body, "diagram-price-empty");
              console.log(
                JSON.stringify({
                  hasBody: Boolean(body),
                  priceTexts: collectText(body),
                  priceValueCount: priceValues.length,
                  priceValueText: priceValues.map((n) => n.textContent).join("|"),
                  emptyCount: priceEmpty.length,
                }),
              );
            }
            """
        ),
    )

    assert output.get("error") is None, output
    assert output["hasBody"] is True
    # 询价块存活(#1 未修:renderMermaid.replaceChildren 抹掉 → priceValueCount 0)
    assert output["priceValueCount"] >= 1, output
    # 价格来自带价来源(#2 未修:命中无价 snapshot 图 → emptyCount 1 且无 100)
    assert output["emptyCount"] == 0, output
    assert "100" in output["priceValueText"], output


_QUESTION_RICH_HARNESS = """
import { renderQuestionRequest } from __BLOCKING_MODULE__;

class Element {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.textContent = "";
    this.className = "";
    this.type = "";
    this._listeners = {};
  }
  append(...children) {
    for (const child of children) {
      if (child !== null && child !== undefined && child !== "") {
        this.children.push(child);
      }
    }
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  click() {
    for (const fn of this._listeners.click || []) {
      fn({ preventDefault() {}, stopPropagation() {} });
    }
  }
  focus() {}
  setAttribute(name, value) {
    this[name] = value;
  }
}

globalThis.document = {
  createElement(tagName) {
    return new Element(tagName);
  },
};

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) {
    walk(child, visit);
  }
}

function findAll(node, predicate) {
  const out = [];
  walk(node, (n) => {
    if (predicate(n)) {
      out.push(n);
    }
  });
  return out;
}
"""


def test_frontend_question_renders_vertical_option_rows(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _QUESTION_RICH_HARNESS
        + textwrap.dedent(
            """
            const answers = [];
            const request = {
              requestId: "ask-1",
              payload: {
                pipeline: true,
                sessionId: "s1",
                question: "请补充部署信息",
                options: [
                  { id: "o1", label: "放一个默认页面即可" },
                  { id: "o2", label: "从 Git 仓库拉取静态文件" },
                  { id: "o3", label: "杭州 cn-hangzhou" },
                ],
                allowFreeText: true,
                freeTextPrompt: "请直接输入答案",
              },
            };
            const panel = renderQuestionRequest(request, {
              onQuestionAnswer: (requestId, answer) => answers.push({ requestId, answer }),
            });
            const rows = findAll(panel, (n) => (n.className || "").split(" ").includes("blocking-option-row"));
            const indices = findAll(panel, (n) => (n.className || "").includes("blocking-option-index")).map(
              (n) => n.textContent,
            );
            const labels = findAll(panel, (n) => (n.className || "").includes("blocking-option-label")).map(
              (n) => n.textContent,
            );
            const horizontalActions = findAll(panel, (n) => (n.className || "").includes("blocking-actions"));
            const textareas = findAll(panel, (n) => n.tagName === "TEXTAREA");
            rows[1].click();
            const selectedAfterClick = findAll(
              panel,
              (n) => (n.className || "").split(" ").includes("is-selected"),
            ).map((n) => n.dataset.optionId);

            console.log(JSON.stringify({
              rowCount: rows.length,
              indices,
              labels,
              hasHorizontalActions: horizontalActions.length > 0,
              textareaCount: textareas.length,
              placeholder: textareas[0]?.placeholder || "",
              answersAfterSelect: answers.length,
              selectedAfterClick,
            }));
            """
        ),
    )

    assert output["rowCount"] == 3
    assert output["indices"] == ["1", "2", "3"]
    assert output["labels"] == [
        "放一个默认页面即可",
        "从 Git 仓库拉取静态文件",
        "杭州 cn-hangzhou",
    ]
    # 竖直选项列表,不再是横向按钮流。
    assert output["hasHorizontalActions"] is False
    assert output["textareaCount"] == 1
    assert output["placeholder"] == "请直接输入答案"
    # allowFreeText 时:点击选项只高亮,不立即提交(留给用户补自由文本后再提交)。
    assert output["answersAfterSelect"] == 0
    assert output["selectedAfterClick"] == ["o2"]


def test_frontend_question_option_click_submits_when_free_text_disabled(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        _QUESTION_RICH_HARNESS
        + textwrap.dedent(
            """
            const answers = [];
            const request = {
              requestId: "ask-9",
              payload: {
                sessionId: "s9",
                question: "选择地域",
                options: [
                  { id: "hz", label: "杭州 cn-hangzhou" },
                  { id: "sh", label: "上海 cn-shanghai" },
                ],
                allowFreeText: false,
              },
            };
            const panel = renderQuestionRequest(request, {
              onQuestionAnswer: (requestId, answer) => answers.push({ requestId, answer }),
            });
            const rows = findAll(panel, (n) => (n.className || "").split(" ").includes("blocking-option-row"));
            rows[1].click();
            const textareas = findAll(panel, (n) => n.tagName === "TEXTAREA");
            console.log(JSON.stringify({ answers, textareaCount: textareas.length }));
            """
        ),
    )

    assert output["answers"] == [
        {
            "requestId": "ask-9",
            "answer": {
                "sessionId": "s9",
                "selected_id": "sh",
                "selected_label": "上海 cn-shanghai",
                "free_text": "",
            },
        }
    ]
    # allowFreeText 关闭时无输入框,点击即提交。
    assert output["textareaCount"] == 0


def test_diagram_optimizing_then_optimized_reducer(tmp_path) -> None:
    # done 但只带 mermaidSource(无 views)→ 包成 1 项 overview 数组,optimizing 清除。
    state = _run_reducer_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        const events = [
          { type: "diagram.optimizing", payload: { candidateIndex: 1, candidateName: "均衡" } },
          {
            type: "diagram.optimized",
            payload: { candidateIndex: 1, candidateName: "均衡", status: "done", mermaidSource: "graph TD\\n  OPT" },
          },
        ];
        let state = {};
        for (const e of events) state = reduceEvent(state, e);
        console.log(JSON.stringify(state));
        """,
    )
    assert "1" not in state["diagramOptimizing"]
    assert state["diagramOptimized"]["1"] == [{"id": "overview", "title": "", "mermaidSource": "graph TD\n  OPT"}]


def test_diagram_optimized_stores_views_array(tmp_path) -> None:
    # done 带 views 数组 → diagramOptimized[idx] 原样存 2 项数组,optimizing 清除。
    state = _run_reducer_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        const events = [
          { type: "diagram.optimizing", payload: { candidateIndex: 1 } },
          {
            type: "diagram.optimized",
            payload: {
              candidateIndex: 1,
              status: "done",
              views: [
                { id: "overview", title: "总览", mermaidSource: "graph TD\\n  A" },
                { id: "network", title: "网络", mermaidSource: "graph TD\\n  B" },
              ],
            },
          },
        ];
        let state = {};
        for (const e of events) state = reduceEvent(state, e);
        console.log(JSON.stringify(state));
        """,
    )
    assert "1" not in state["diagramOptimizing"]
    assert state["diagramOptimized"]["1"] == [
        {"id": "overview", "title": "总览", "mermaidSource": "graph TD\n  A"},
        {"id": "network", "title": "网络", "mermaidSource": "graph TD\n  B"},
    ]


def test_diagram_optimizing_marks_in_progress(tmp_path) -> None:
    state = _run_reducer_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        const state = reduceEvent({}, {
          type: "diagram.optimizing",
          payload: { candidateIndex: 1, candidateName: "均衡" },
        });
        console.log(JSON.stringify(state));
        """,
    )
    assert state["diagramOptimizing"]["1"] is True


def test_diagram_optimized_failed_only_clears_optimizing(tmp_path) -> None:
    state = _run_reducer_script(
        tmp_path,
        """
        import { reduceEvent } from __EVENTS_MODULE__;
        const events = [
          { type: "diagram.optimizing", payload: { candidateIndex: 0 } },
          { type: "diagram.optimized", payload: { candidateIndex: 0, status: "failed" } },
        ];
        let state = {};
        for (const e of events) state = reduceEvent(state, e);
        console.log(JSON.stringify(state));
        """,
    )
    assert "0" not in state["diagramOptimizing"]
    assert "0" not in (state.get("diagramOptimized") or {})


_MERMAID_VIEWS_ELEMENT = """
class Element {
  constructor(tag) {
    this.tagName = (tag || "").toUpperCase();
    this.children = [];
    this.dataset = {};
    this.className = "";
    this.textContent = "";
    this.type = "";
    this._handlers = {};
    this._innerHTML = "";
  }
  append(...c) { this.children.push(...c); }
  replaceChildren(...c) { this.children = [...c]; }
  addEventListener(name, fn) { (this._handlers[name] ||= []).push(fn); }
  __click() { (this._handlers.click || []).forEach((fn) => fn()); }
  set innerHTML(value) { this._innerHTML = value; }
  get innerHTML() { return this._innerHTML || ""; }
}

function collectByClass(node, cls, out = []) {
  if (node && typeof node.className === "string" && node.className.split(" ").includes(cls)) {
    out.push(node);
  }
  for (const child of node?.children || []) {
    collectByClass(child, cls, out);
  }
  return out;
}
"""


def test_render_mermaid_views_builds_tabs_and_switches_active(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderMermaidViews } from __MERMAID_MODULE__;

            __ELEMENT__

            globalThis.document = {
              createElement: (tag) => new Element(tag),
              documentElement: { getAttribute: () => null },
            };
            globalThis.window = {
              mermaid: {
                initialize: () => {},
                render: async (id, source) => ({ svg: "<svg data-src='" + source + "'></svg>" }),
              },
            };

            const container = new Element("div");
            await renderMermaidViews(container, [
              { id: "overview", title: "总览", mermaidSource: "graph TD\\n  A" },
              { id: "network", title: "网络", mermaidSource: "graph TD\\n  B" },
            ]);

            const tabsWrap = collectByClass(container, "diagram-view-tabs");
            const bodyWrap = collectByClass(container, "diagram-view-body");
            const tabs = collectByClass(container, "diagram-view-tab");
            const initialBody = bodyWrap[0].children.map((c) => c.innerHTML);
            const initialTabClasses = tabs.map((t) => t.className);

            tabs[1].__click();
            await new Promise((resolve) => setTimeout(resolve, 0));

            const afterClickBody = bodyWrap[0].children.map((c) => c.innerHTML);
            const afterTabClasses = tabs.map((t) => t.className);
            console.log(JSON.stringify({
              tabsWrapCount: tabsWrap.length,
              tabCount: tabs.length,
              tabLabels: tabs.map((t) => t.textContent),
              initialTabClasses,
              afterTabClasses,
              initialBody,
              afterClickBody,
            }));
            """
        ).replace("__ELEMENT__", _MERMAID_VIEWS_ELEMENT),
    )

    assert output["tabsWrapCount"] == 1
    assert output["tabCount"] == 2
    assert output["tabLabels"] == ["总览", "网络"]
    # 首个默认 is-active。
    assert "is-active" in output["initialTabClasses"][0]
    assert "is-active" not in output["initialTabClasses"][1]
    # 初始渲染视图 0。
    assert output["initialBody"] == ["<svg data-src='graph TD\n  A'></svg>"]
    # 点第 2 个标签 → 迁移 is-active,并渲染视图 1。
    assert "is-active" not in output["afterTabClasses"][0]
    assert "is-active" in output["afterTabClasses"][1]
    assert output["afterClickBody"] == ["<svg data-src='graph TD\n  B'></svg>"]


def test_render_mermaid_views_single_view_has_no_tabs(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            import { renderMermaidViews } from __MERMAID_MODULE__;

            __ELEMENT__

            globalThis.document = {
              createElement: (tag) => new Element(tag),
              documentElement: { getAttribute: () => null },
            };
            globalThis.window = {
              mermaid: {
                initialize: () => {},
                render: async (id, source) => ({ svg: "<svg data-src='" + source + "'></svg>" }),
              },
            };

            const container = new Element("div");
            await renderMermaidViews(container, [
              { id: "overview", title: "", mermaidSource: "graph TD\\n  ONLY" },
            ]);

            const svgs = [];
            (function walk(node) {
              if (node.innerHTML) svgs.push(node.innerHTML);
              for (const child of node.children || []) walk(child);
            })(container);
            console.log(JSON.stringify({
              tabsWrapCount: collectByClass(container, "diagram-view-tabs").length,
              tabCount: collectByClass(container, "diagram-view-tab").length,
              svgs,
            }));
            """
        ).replace("__ELEMENT__", _MERMAID_VIEWS_ELEMENT),
    )

    assert output["tabsWrapCount"] == 0
    assert output["tabCount"] == 0
    assert output["svgs"] == ["<svg data-src='graph TD\n  ONLY'></svg>"]


def test_overlay_marks_optimizing_and_swaps_optimized(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null, createElement: (t) => ({ tag: t }) };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { overlayDiagramOptimization } = await import(__APP_MODULE__);
            const diagrams = [
              { candidateIndex: 0, mermaidSource: "graph TD\\n  DRAFT0", optimized: false },
              { candidateIndex: 1, mermaidSource: "graph TD\\n  DRAFT1", optimized: false },
            ];
            const marking = overlayDiagramOptimization(
              diagrams, { diagramOptimizing: { "1": true }, diagramOptimized: {} });
            const swapping = overlayDiagramOptimization(
              diagrams, { diagramOptimizing: {}, diagramOptimized: {
                "1": [
                  { id: "overview", title: "Overview", mermaidSource: "graph TD\\n  OPT1" },
                  { id: "network", title: "Network", mermaidSource: "graph TD\\n  OPT1_NET" },
                ],
              } });
            const byIdx = (arr) => Object.fromEntries(arr.map((d) => [d.candidateIndex, d]));
            console.log(JSON.stringify({ marking: byIdx(marking), swapping: byIdx(swapping) }));
            """
        ),
    )
    marking = output["marking"]
    assert marking["1"]["optimizing"] is True
    assert marking["0"]["optimizing"] is False
    swapping = output["swapping"]
    # Task 4 shape: diagramOptimized[idx] is a views array; overlay lands it on `views`
    # and mirrors views[0].mermaidSource onto mermaidSource for single-view render paths.
    assert swapping["1"]["mermaidSource"] == "graph TD\n  OPT1"
    assert len(swapping["1"]["views"]) == 2
    assert swapping["1"]["views"][1]["mermaidSource"] == "graph TD\n  OPT1_NET"
    assert swapping["1"]["optimized"] is True
    assert swapping["1"]["optimizing"] is False


def test_pipeline_step_shows_optimizing_badge_on_that_candidate(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);
            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            const diagrams = [
              { candidateName: "方案A", diagramId: "d0", candidateIndex: 0, format: "mermaid", optimizing: true },
              { candidateName: "方案B", diagramId: "d1", candidateIndex: 1, format: "mermaid", optimizing: false },
            ];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: () => true,
              onSelectCandidate: () => {},
            });
            const badges = collectByClass(group.diagramGroup, "diagram-optimizing");
            console.log(JSON.stringify({ badgeCount: badges.length, badgeText: badges.map((b) => b.textContent) }));
            """
        ),
    )
    assert output["badgeCount"] == 1
    assert output["badgeText"] == ["Optimizing"]


def test_pipeline_step_shows_pending_optimizing_and_no_badge_states(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };

            class Element {
              constructor(tag) {
                this.tagName = (tag || "").toUpperCase();
                this.children = [];
                this.dataset = {};
                this.className = "";
                this.textContent = "";
                this.type = "";
                this._handlers = {};
              }
              append(...c) { this.children.push(...c); }
              addEventListener(type, fn) { (this._handlers[type] ||= []).push(fn); }
              setAttribute() {}
              __click() { (this._handlers.click || []).forEach((fn) => fn()); }
            }

            function collectByClass(node, cls, out = []) {
              if (node && typeof node.className === "string" && node.className.includes(cls)) {
                out.push(node);
              }
              for (const child of node?.children || []) {
                collectByClass(child, cls, out);
              }
              return out;
            }

            const { renderPipelineMarkerGroup } = await import(__APP_MODULE__);
            const message = { kind: "pipeline_step", pipelineStep: { stepId: "confirm_and_select", status: "input" } };
            // idx0 正在优化 → 优化中；idx1 已识别草图但尚未优化 → 待优化；idx2 已优化完成 → 无徽标。
            const base = { format: "mermaid", diagramId: "d" };
            const diagrams = [
              { ...base, candidateName: "方案A", candidateIndex: 0, optimizing: true, optimized: false },
              { ...base, candidateName: "方案B", candidateIndex: 1, optimizing: false, optimized: false },
              { ...base, candidateName: "方案C", candidateIndex: 2, optimizing: false, optimized: true },
            ];
            const group = renderPipelineMarkerGroup(message, {
              diagrams,
              toggleDiagram: () => true,
              onSelectCandidate: () => {},
            });
            const optimizing = collectByClass(group.diagramGroup, "diagram-optimizing");
            const pending = collectByClass(group.diagramGroup, "diagram-pending");
            console.log(JSON.stringify({
              optimizingText: optimizing.map((b) => b.textContent),
              pendingText: pending.map((b) => b.textContent),
            }));
            """
        ),
    )
    # 优化中 只出现在 idx0；待优化 只出现在 idx1；idx2(已优化)两类徽标都不出现。
    assert output["optimizingText"] == ["Optimizing"]
    assert output["pendingText"] == ["Pending optimization"]


def test_diagram_optimization_state_maps_all_cases(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null, createElement: (t) => ({ tag: t }) };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { diagramOptimizationState } = await import(__APP_MODULE__);
            const state = { diagramOptimizing: { "0": true }, diagramOptimized: { "3": [{ mermaidSource: "x" }] } };
            console.log(JSON.stringify({
              // idx0 事件在途 → optimizing（即便后端 optimized=true，进行中优先）
              optimizing: diagramOptimizationState({ candidateIndex: 0, optimized: true }, state),
              // idx1 草图：无事件、后端 optimized=false → pending
              pending: diagramOptimizationState({ candidateIndex: 1, optimized: false }, state),
              // idx2 后端缓存命中 optimized=true、无事件 → done
              doneByFlag: diagramOptimizationState({ candidateIndex: 2, optimized: true }, state),
              // idx3 本轮 optimized 事件产出 → done（即便 item.optimized 未带）
              doneByEvent: diagramOptimizationState({ candidateIndex: 3 }, state),
              // 无 candidateIndex（部署产物等）→ none
              noneNull: diagramOptimizationState({ candidateIndex: null, optimized: false }, state),
              noneUndef: diagramOptimizationState({ optimized: false }, state),
            }));
            """
        ),
    )
    assert output["optimizing"] == "optimizing"
    assert output["pending"] == "pending"
    assert output["doneByFlag"] == "done"
    assert output["doneByEvent"] == "done"
    assert output["noneNull"] == "none"
    assert output["noneUndef"] == "none"


def test_diagram_optimization_state_honors_backend_inflight_after_resync(tmp_path) -> None:
    # 回归 step4 徽标倒退 优化中 → 待优化 → 完成:resync 清空事件态(空 diagramOptimizing/
    # diagramOptimized)后,仍在优化的候选此前只剩后端 optimized=false 可依据 → 误判 pending(待优化)。
    # 现 /outputs 带后端权威 optimizing 标志,状态应恢复为 optimizing(优化中),不再倒退。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null, createElement: (t) => ({ tag: t }) };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { diagramOptimizationState } = await import(__APP_MODULE__);
            const emptyEvents = { diagramOptimizing: {}, diagramOptimized: {} };
            console.log(JSON.stringify({
              // resync 后事件态空,但后端报 optimizing:true(缓存尚未写) → optimizing(不倒退成 pending)
              backendInflight: diagramOptimizationState(
                { candidateIndex: 0, optimized: false, optimizing: true }, emptyEvents),
              // 后端缓存已 done,即便 inflight 标志尚未清 → done 优先
              backendDoneWins: diagramOptimizationState(
                { candidateIndex: 1, optimized: true, optimizing: true }, emptyEvents),
              // 后端既非 done 也非 inflight → pending
              stillPending: diagramOptimizationState(
                { candidateIndex: 2, optimized: false, optimizing: false }, emptyEvents),
            }));
            """
        ),
    )
    assert output["backendInflight"] == "optimizing"
    assert output["backendDoneWins"] == "done"
    assert output["stillPending"] == "pending"


def test_overlay_honors_backend_inflight_after_resync(tmp_path) -> None:
    # 覆盖层同样:事件态被 resync 清空后,后端 optimizing 标志须存活(else 分支曾无条件置 false)。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null, createElement: (t) => ({ tag: t }) };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { overlayDiagramOptimization } = await import(__APP_MODULE__);
            const diagrams = [
              { candidateIndex: 0, mermaidSource: "graph TD\\n  D0", optimized: false, optimizing: true },
              { candidateIndex: 1, mermaidSource: "graph TD\\n  D1", optimized: true, optimizing: true },
              { candidateIndex: 2, mermaidSource: "graph TD\\n  D2", optimized: false, optimizing: false },
            ];
            const out = overlayDiagramOptimization(diagrams, { diagramOptimizing: {}, diagramOptimized: {} });
            const byIdx = Object.fromEntries(out.map((d) => [d.candidateIndex, d]));
            console.log(JSON.stringify({
              inflight: byIdx["0"].optimizing,
              doneWins: byIdx["1"].optimizing,
              none: byIdx["2"].optimizing,
            }));
            """
        ),
    )
    # 后端 inflight → 保留 optimizing:true
    assert output["inflight"] is True
    # 后端已 done → 不再标 optimizing(避免完成后仍显示优化中)
    assert output["doneWins"] is False
    assert output["none"] is False


def test_diagram_state_badge_renders_by_state(tmp_path) -> None:
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => ({ tag, className: "", textContent: "" }),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { diagramStateBadge } = await import(__OUTPUT_PANEL_MODULE__);
            const describe = (b) => (b === null ? null : { className: b.className, textContent: b.textContent });
            console.log(JSON.stringify({
              pending: describe(diagramStateBadge("pending")),
              optimizing: describe(diagramStateBadge("optimizing")),
              done: describe(diagramStateBadge("done")),
              none: describe(diagramStateBadge("none")),
            }));
            """
        ),
    )
    assert output["pending"] == {"className": "diagram-pending", "textContent": "Pending optimization"}
    assert output["optimizing"] == {"className": "diagram-optimizing", "textContent": "Optimizing"}
    # done / none 不挂徽标。
    assert output["done"] is None
    assert output["none"] is None


def test_pipeline_thinking_label_rotates_every_three_seconds_with_elapsed(tmp_path) -> None:
    # 文案每 3 秒在【处理中/执行中/进行中/运行中】间轮换,附已等待整秒数;负值归零、12s 回环到首词。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { pipelineThinkingLabel } = await import(__APP_MODULE__);
            const samples = [-1000, 0, 2999, 3000, 6000, 9000, 12000];
            console.log(JSON.stringify(samples.map((ms) => pipelineThinkingLabel(ms))));
            """
        ),
    )
    assert output == [
        "Processing… 0s",  # 负值归零
        "Processing… 0s",
        "Processing… 2s",  # 未满 3s 仍首词
        "Executing… 3s",  # 3s 换第二词
        "In progress… 6s",
        "Running… 9s",
        "Processing… 12s",  # floor(12/3)%4=0 回环到首词
    ]


def test_sync_pipeline_thinking_injects_per_leaf_skips_parent_and_active(tmp_path) -> None:
    # syncPipelineThinking 只给「进行中叶子」补一枚流光占位:含更深 working 子步骤的父步骤让位;
    # 步骤内已有实时活动(.tool-card.is-active 等)时不补。用富 Element 桩驱动真实 DOM 逻辑。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            class Element {
              constructor(tag = "div") {
                this.tagName = String(tag).toUpperCase();
                this.className = "";
                this.textContent = "";
                this.dataset = {};
                this.children = [];
                this.parent = null;
                this.style = {};
              }
              get classList() { return this.className ? this.className.split(/\\s+/) : []; }
              append(...nodes) {
                for (const node of nodes) { node.parent = this; this.children.push(node); }
              }
              remove() {
                if (this.parent) {
                  this.parent.children = this.parent.children.filter((c) => c !== this);
                  this.parent = null;
                }
              }
              contains(node) {
                let p = node.parent;
                while (p) { if (p === this) { return true; } p = p.parent; }
                return false;
              }
              _descendants(out = []) {
                for (const c of this.children) { out.push(c); c._descendants(out); }
                return out;
              }
              _matchesChain(token) {
                const classes = token.trim().split(".").filter(Boolean);
                return classes.every((cls) => this.classList.includes(cls));
              }
              querySelector(sel) {
                if (sel.startsWith(":scope >")) {
                  const rest = sel.slice(":scope >".length).trim();
                  return this.children.find((c) => c._matchesChain(rest)) || null;
                }
                const tokens = sel.split(",");
                for (const node of this._descendants()) {
                  if (tokens.some((t) => node._matchesChain(t))) { return node; }
                }
                return null;
              }
              querySelectorAll(sel) {
                // 仅支持 [data-step-status="working"]。
                const m = sel.match(/\\[data-step-status="(.+)"\\]/);
                const want = m ? m[1] : null;
                return this._descendants().filter((n) => n.dataset.stepStatus === want);
              }
            }

            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { syncPipelineThinking } = await import(__APP_MODULE__);

            const mkBody = (key) => {
              const b = new Element("div");
              b.className = "pipeline-step-body";
              b.dataset.stepStatus = "working";
              b.dataset.stepKey = key;
              return b;
            };
            const root = new Element("div");
            const leaf = mkBody("k-leaf");            // 进行中叶子,无活动 → 应补占位
            const parent = mkBody("k-parent");        // 含更深 working 子 → 让位,不补
            const child = mkBody("k-child");          // parent 的 working 子叶子 → 应补占位
            parent.append(child);
            const busy = mkBody("k-busy");            // 有实时活动 → 不补
            const tool = new Element("div");
            tool.className = "tool-card is-active";
            busy.append(tool);
            root.append(leaf, parent, busy);

            syncPipelineThinking(root);

            const countThinking = (b) => b.children.filter((c) => c.className.includes("pipeline-thinking")).length;
            const labelOf = (b) => {
              const t = b.children.find((c) => c.className.includes("pipeline-thinking"));
              return t ? t.children[0].textContent : null;
            };
            console.log(JSON.stringify({
              leaf: countThinking(leaf),
              leafLabel: labelOf(leaf),
              parent: countThinking(parent),
              child: countThinking(child),
              busy: countThinking(busy),
            }));
            """
        ),
    )
    assert output["leaf"] == 1  # 叶子补一枚
    assert output["leafLabel"] == "Processing… 0s"  # 刚进入静默,elapsed≈0
    assert output["parent"] == 0  # 父步骤让位给进行中子步骤
    assert output["child"] == 1  # 子叶子补一枚
    assert output["busy"] == 0  # 有实时活动不补


def test_sync_pipeline_thinking_injects_when_streaming_flag_is_stale(tmp_path) -> None:
    # 核心 bug 回归:流水线段消息的 .message-agent.is-streaming 会挂到整步结束,正文吐完、后端静默后
    # 仍在。旧逻辑把它当实时活动 → 占位被永久压制(用户看到「界面卡很」无任何进度指示)。修复后:该
    # 标记仅当最近 PIPELINE_STREAM_SILENCE_MS 内有 delta 才算流式;新导入时 lastStreamDeltaAt=0(远早于
    # now)判为停顿 → 占位得以出现。这里不打任何 delta,复刻静默间隙。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            class Element {
              constructor(tag = "div") {
                this.tagName = String(tag).toUpperCase();
                this.className = "";
                this.textContent = "";
                this.dataset = {};
                this.children = [];
                this.parent = null;
                this.style = {};
              }
              get classList() { return this.className ? this.className.split(/\\s+/) : []; }
              append(...nodes) {
                for (const node of nodes) { node.parent = this; this.children.push(node); }
              }
              remove() {
                if (this.parent) {
                  this.parent.children = this.parent.children.filter((c) => c !== this);
                  this.parent = null;
                }
              }
              contains(node) {
                let p = node.parent;
                while (p) { if (p === this) { return true; } p = p.parent; }
                return false;
              }
              _descendants(out = []) {
                for (const c of this.children) { out.push(c); c._descendants(out); }
                return out;
              }
              _matchesChain(token) {
                const classes = token.trim().split(".").filter(Boolean);
                return classes.every((cls) => this.classList.includes(cls));
              }
              querySelector(sel) {
                if (sel.startsWith(":scope >")) {
                  const rest = sel.slice(":scope >".length).trim();
                  return this.children.find((c) => c._matchesChain(rest)) || null;
                }
                const tokens = sel.split(",");
                for (const node of this._descendants()) {
                  if (tokens.some((t) => node._matchesChain(t))) { return node; }
                }
                return null;
              }
              querySelectorAll(sel) {
                const m = sel.match(/\\[data-step-status="(.+)"\\]/);
                const want = m ? m[1] : null;
                return this._descendants().filter((n) => n.dataset.stepStatus === want);
              }
            }

            globalThis.document = {
              getElementById: () => null,
              createElement: (tag) => new Element(tag),
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { syncPipelineThinking } = await import(__APP_MODULE__);

            const root = new Element("div");
            const leaf = new Element("div");
            leaf.className = "pipeline-step-body";
            leaf.dataset.stepStatus = "working";
            leaf.dataset.stepKey = "k-stale";
            // 段消息正文吐完仍挂 is-streaming(流水线里步骤 completed 前不落 message.end)。
            const streamed = new Element("article");
            streamed.className = "message message-agent is-streaming";
            leaf.append(streamed);
            root.append(leaf);

            syncPipelineThinking(root);

            const thinking = leaf.children.filter((c) => c.className.includes("pipeline-thinking"));
            console.log(JSON.stringify({
              count: thinking.length,
              label: thinking.length ? thinking[0].children[0].textContent : null,
            }));
            """
        ),
    )
    assert output["count"] == 1  # 陈旧 is-streaming 不再压制,占位出现
    assert output["label"] == "Processing… 0s"


def _normal_thinking_stub() -> str:
    # syncNormalThinking / stepBodyHasLiveActivity 需要的最小富 Element 桩:支持 :scope >、逗号选择器、
    # 后代查找与 append/remove。querySelectorAll 不被 syncNormalThinking 调用,给个宽松实现即可。
    return textwrap.dedent(
        """
        class Element {
          constructor(tag = "div") {
            this.tagName = String(tag).toUpperCase();
            this.className = "";
            this.textContent = "";
            this.dataset = {};
            this.children = [];
            this.parent = null;
            this.style = {};
          }
          get classList() { return this.className ? this.className.split(/\\s+/) : []; }
          append(...nodes) {
            for (const node of nodes) { node.parent = this; this.children.push(node); }
          }
          remove() {
            if (this.parent) {
              this.parent.children = this.parent.children.filter((c) => c !== this);
              this.parent = null;
            }
          }
          contains(node) {
            let p = node.parent;
            while (p) { if (p === this) { return true; } p = p.parent; }
            return false;
          }
          _descendants(out = []) {
            for (const c of this.children) { out.push(c); c._descendants(out); }
            return out;
          }
          _matchesChain(token) {
            const classes = token.trim().split(".").filter(Boolean);
            return classes.every((cls) => this.classList.includes(cls));
          }
          querySelector(sel) {
            if (sel.startsWith(":scope >")) {
              const rest = sel.slice(":scope >".length).trim();
              return this.children.find((c) => c._matchesChain(rest)) || null;
            }
            const tokens = sel.split(",");
            for (const node of this._descendants()) {
              if (tokens.some((t) => node._matchesChain(t))) { return node; }
            }
            return null;
          }
          querySelectorAll() { return []; }
        }

        globalThis.document = {
          getElementById: () => null,
          createElement: (tag) => new Element(tag),
        };
        globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
        """
    )


def test_sync_normal_thinking_injects_bottom_placeholder_when_idle(tmp_path) -> None:
    # 普通模式活跃回合的静默间隙(无内联工具/流式/思考)→ 在 message-stack 底部补一枚流光占位,
    # label 为「处理中… 0s」(刚进入静默,elapsed≈0)。byShell 在测试里取不到独立工具活动区(root 未设)。
    output = _run_reducer_script(
        tmp_path,
        _normal_thinking_stub()
        + textwrap.dedent(
            """
            const { syncNormalThinking } = await import(__APP_MODULE__);

            const stack = new Element("div");
            const done = new Element("article");
            done.className = "message message-agent";  // 已完成消息,非流式 → 不算实时活动
            stack.append(done);

            syncNormalThinking(stack);

            const thinking = stack.children.filter((c) => c.className.includes("pipeline-thinking"));
            console.log(JSON.stringify({
              count: thinking.length,
              label: thinking.length ? thinking[0].children[0].textContent : null,
            }));
            """
        ),
    )
    assert output["count"] == 1  # 静默间隙补一枚底部占位
    assert output["label"] == "Processing… 0s"


def test_sync_normal_thinking_suppressed_by_active_tool(tmp_path) -> None:
    # 普通模式正文区里有进行中工具卡(.tool-card.is-active)属实时活动 → 不补底部占位。
    output = _run_reducer_script(
        tmp_path,
        _normal_thinking_stub()
        + textwrap.dedent(
            """
            const { syncNormalThinking } = await import(__APP_MODULE__);

            const stack = new Element("div");
            const msg = new Element("article");
            msg.className = "message message-agent";
            const tool = new Element("div");
            tool.className = "tool-card is-active";
            msg.append(tool);
            stack.append(msg);

            syncNormalThinking(stack);

            const thinking = stack.children.filter((c) => c.className.includes("pipeline-thinking"));
            console.log(JSON.stringify({ count: thinking.length }));
            """
        ),
    )
    assert output["count"] == 0  # 有实时活动不补


def test_make_new_session_draft_reads_injected_defaults(tmp_path) -> None:
    # 首屏注入到 <body> 的默认(权限/模式/流水线 flavor)应被新会话草稿采用;
    # 显式入参优先于注入默认。Node 侧在 import 前设置 document.body.dataset,
    # 让模块级 SESSION_DEFAULTS 读到注入值。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              body: {
                dataset: {
                  defaultPermissionMode: "accept_edits",
                  defaultMode: "pipeline",
                  defaultPipelineName: "selling",
                },
              },
              getElementById: () => null,
            };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { makeNewSessionDraft } = await import(__APP_MODULE__);

            const fromDefaults = makeNewSessionDraft();
            const explicitOverride = makeNewSessionDraft({ permissionMode: "dont_ask", mode: "normal" });

            console.log(JSON.stringify({
              defaultPermission: fromDefaults.permissionMode,
              defaultMode: fromDefaults.mode,
              defaultPipeline: fromDefaults.pipelineName,
              overridePermission: explicitOverride.permissionMode,
              overrideMode: explicitOverride.mode,
            }));
            """
        ),
    )

    assert output == {
        "defaultPermission": "accept_edits",
        "defaultMode": "pipeline",
        "defaultPipeline": "selling",
        "overridePermission": "dont_ask",
        "overrideMode": "normal",
    }


def test_make_new_session_draft_falls_back_without_body_dataset(tmp_path) -> None:
    # 无 body dataset(如静态测试环境)时回落安全默认:权限=default、模式=normal。
    output = _run_reducer_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById: () => null };
            globalThis.window = { location: { hostname: "localhost", origin: "http://localhost" } };
            const { makeNewSessionDraft } = await import(__APP_MODULE__);

            const draft = makeNewSessionDraft();
            console.log(JSON.stringify({
              permissionMode: draft.permissionMode,
              mode: draft.mode,
            }));
            """
        ),
    )

    assert output == {"permissionMode": "default", "mode": "normal"}
