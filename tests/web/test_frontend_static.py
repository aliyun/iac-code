import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

EVENTS_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/events.js"
API_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/api.js"
APP_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/app.js"
I18N_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/i18n.js"
COMPOSER_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/composer.js"
TOKEN_TRANSPORT_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/token_transport.js"
TOKEN_CRYPTO_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/vendor/token-crypto.js"
BLOCKING_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/blocking.js"
PIPELINE_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/pipeline.js"
WORKSPACE_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/workspace.js"
TOOL_CARDS_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js"
OUTPUT_PANEL_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/output_panel.js"
MERMAID_RENDER_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/mermaid_render.js"
MERMAID_VENDOR_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/vendor/mermaid.min.js"
INDEX_HTML = Path(__file__).parents[2] / "src/iac_code/web/static/index.html"
STYLES_CSS = Path(__file__).parents[2] / "src/iac_code/web/static/styles.css"
VISUAL_AUDIT_SCRIPT = Path(__file__).parents[2] / "scripts/web/e2e/web_repl_visual_audit.mjs"
SMOKE_SCRIPT = Path(__file__).parents[2] / "scripts/web/e2e/web_repl_smoke.mjs"

REQUIRED_WEB_EVENT_TYPES = [
    "session.started",
    "session.updated",
    "session.resync.required",
    "user.message",
    "assistant.message.start",
    "assistant.text.delta",
    "assistant.thinking.delta",
    "assistant.message.tombstone",
    "assistant.message.end",
    "tool.started",
    "tool.input.delta",
    "tool.progress",
    "tool.result",
    "tool.finished",
    "subagent.event",
    "permission.request",
    "permission.resolved",
    "question.request",
    "question.resolved",
    "elicitation.request",
    "elicitation.resolved",
    "queued-input.accepted",
    "queued-input.submitted",
    "draft.updated",
    "interrupt.accepted",
    "command.started",
    "command.finished",
    "compaction.started",
    "compaction.finished",
    "mcp.status.updated",
    "task.notification",
    "resource.observed",
    "plan.updated",
    "debug.stream_event",
    "local.shell.start",
    "local.shell.end",
    "pipeline.event",
    "pipeline.snapshot",
    "pipeline.step.marker",
    "candidate.detail",
    "diagram.render",
    "cleanup.status",
    "error",
    "turn.done",
]


def _events_js_source() -> str:
    assert EVENTS_JS.exists(), "events.js must define the web event reducer"
    return EVENTS_JS.read_text(encoding="utf-8")


def _source(path: Path) -> str:
    assert path.exists(), f"{path.name} must exist for the Web frontend"
    return path.read_text(encoding="utf-8")


def _css_block(source: str, selector: str) -> str:
    escaped = re.escape(selector)
    match = re.search(rf"{escaped}\s*\{{(?P<body>.*?)\n\}}", source, re.DOTALL)
    assert match is not None, f"{selector} block should exist"
    return match.group("body")


def _run_workspace_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "workspace-test.mjs"
    script_source = source.strip().replace("__WORKSPACE_MODULE__", json.dumps(WORKSPACE_JS.as_uri()))
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_api_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "api-test.mjs"
    script_source = (
        source.strip()
        .replace("__API_MODULE__", json.dumps(API_JS.as_uri()))
        .replace("__EVENTS_MODULE__", json.dumps(EVENTS_JS.as_uri()))
    )
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_events_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "events-test.mjs"
    script_source = source.strip().replace("__EVENTS_MODULE__", json.dumps(EVENTS_JS.as_uri()))
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_app_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "app-test.mjs"
    script_source = source.strip().replace("__APP_MODULE__", json.dumps(APP_JS.as_uri()))
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_toolcards_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "toolcards-test.mjs"
    script_source = source.strip().replace("__TOOLCARDS_MODULE__", json.dumps(TOOL_CARDS_JS.as_uri()))
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_composer_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "composer-test.mjs"
    script_source = source.strip().replace("__COMPOSER_MODULE__", json.dumps(COMPOSER_JS.as_uri()))
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_output_panel_script(tmp_path: Path, source: str) -> dict[str, object]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script = tmp_path / "output-panel-test.mjs"
    script_source = source.strip().replace("__OUTPUT_PANEL_MODULE__", json.dumps(OUTPUT_PANEL_JS.as_uri()))
    script.write_text(script_source, encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reducer_uses_payload_delta_for_assistant_text_delta() -> None:
    source = _events_js_source()

    assert "assistant.text.delta" in source
    assert "payload.delta" in source


def test_reducer_deletes_tombstoned_messages_and_tools() -> None:
    source = _events_js_source()

    assert "assistant.message.tombstone" in source
    assert "affectedToolUseIds" in source
    assert "delete next.messages[payload.messageId]" in source
    assert "delete next.tools[toolUseId]" in source


def test_reducer_handles_tool_started_and_nested_children() -> None:
    source = _events_js_source()

    assert "tool.started" in source
    assert "parentToolUseId" in source
    assert "children" in source


def test_reducer_resets_stored_seeds_before_replaying_append_deltas() -> None:
    # 同进程 reload：磁盘快照先把整段正文/工具 input 塞进 state（stored=true），随后事件缓冲区
    # 又从 floor 回放本轮的 append 型 delta（assistant.text.delta / tool.input.delta / tool.result）。
    # 若不在 start 时清空快照，正文与 input 会翻倍拼接——complete_step 的 input 变成两段 JSON
    # 拼接而解析失败，结论退回展示原始 JSON。收到 start 即代表后续 delta 会完整重建，需先清空。
    source = _events_js_source()

    # assistant.message.start 分支清空 stored 消息正文。
    start_index = source.index('case "assistant.message.start"')
    start_block = source[start_index : start_index + 600]
    assert "message.stored" in start_block
    assert 'message.text = ""' in start_block
    assert "message.stored = false" in start_block

    # tool.started 分支清空 stored 工具的 input/results/artifacts。
    tool_index = source.index('case "tool.started"')
    tool_block = source[tool_index : tool_index + 600]
    assert "tool.stored" in tool_block
    assert 'tool.input = ""' in tool_block
    assert "tool.results = []" in tool_block
    assert "tool.stored = false" in tool_block


def test_user_message_reducer_preserves_seeded_transcript_sequence(tmp_path) -> None:
    # Issue 7：同进程 reload 时，A2A 种子先给用户气泡定了正确的转录序号（stored，seq=1）。
    # 随后事件缓冲区又从 floor 回放本轮 live 的 user.message（其 web 序号更大，如 4）。旧代码
    # 无条件用 event.sequence 覆盖，把用户气泡挪到流水线步骤之后、错位嵌进步骤体内。现在序号
    # 一旦确定即为锚点，不再被回放改写；仅尚无序号的全新气泡才采纳 event.sequence。
    output = _run_events_script(
        tmp_path,
        textwrap.dedent(
            """
            const { reduceEvent } = await import(__EVENTS_MODULE__);

            const seeded = {
              messages: {
                "user-T": {
                  messageId: "user-T", role: "user", text: "帮我用 ROS 部署...", content: "帮我用 ROS 部署...",
                  status: "completed", sequence: 1, stored: true, toolUseIds: [],
                },
              },
              tools: {}, turns: {}, lastSequence: 0,
            };

            // reload 回放：同一 messageId、更大的 web 序号 4。
            const afterReplay = reduceEvent(seeded, {
              sequence: 4, type: "user.message",
              payload: { messageId: "user-T", turnId: "T", text: "帮我用 ROS 部署..." },
            });

            // 全新用户气泡（无种子序号）应采纳事件序号 5。
            const withFresh = reduceEvent(afterReplay, {
              sequence: 5, type: "user.message",
              payload: { messageId: "user-NEW", turnId: "T2", text: "新的一条" },
            });

            console.log(JSON.stringify({
              seededSequence: withFresh.messages["user-T"].sequence,
              freshSequence: withFresh.messages["user-NEW"].sequence,
            }));
            """
        ),
    )

    # 种子序号被保住（1，不是回放的 4）；全新气泡采纳事件序号（5）。
    assert output["seededSequence"] == 1
    assert output["freshSequence"] == 5


def test_reducer_reuses_persisted_message_ids_during_live_replay(tmp_path) -> None:
    # 同进程 reload 会先装载磁盘快照，再从事件缓冲区回放同一轮实时事件。两条路径必须用同一
    # messageId：用户气泡不能重复，assistant.start 必须原位清空快照并由 delta 重建正文。
    output = _run_events_script(
        tmp_path,
        textwrap.dedent(
            """
            const { reduceEvent } = await import(__EVENTS_MODULE__);

            let state = {
              messages: {
                "user-turn-1": {
                  messageId: "user-turn-1", role: "user", text: "创建 VPC", content: "创建 VPC",
                  status: "completed", sequence: 1, stored: true, toolUseIds: [],
                },
                "assistant-provider-1": {
                  messageId: "assistant-provider-1", role: "assistant", text: "完整回复",
                  content: "完整回复", status: "completed", sequence: 2, stored: true, toolUseIds: [],
                },
              },
              tools: {}, turns: {}, lastSequence: 0,
            };

            state = reduceEvent(state, {
              sequence: 10, type: "user.message",
              payload: { messageId: "user-turn-1", turnId: "turn-1", text: "创建 VPC" },
            });
            state = reduceEvent(state, {
              sequence: 11, type: "assistant.message.start",
              payload: { messageId: "assistant-provider-1", turnId: "turn-1" },
            });
            state = reduceEvent(state, {
              sequence: 12, type: "assistant.text.delta",
              payload: { messageId: "assistant-provider-1", turnId: "turn-1", delta: "完整回复" },
            });
            state = reduceEvent(state, {
              sequence: 13, type: "assistant.message.end",
              payload: { messageId: "assistant-provider-1", turnId: "turn-1" },
            });

            console.log(JSON.stringify({
              messageIds: Object.keys(state.messages).sort(),
              userText: state.messages["user-turn-1"].text,
              assistantText: state.messages["assistant-provider-1"].text,
              assistantStored: state.messages["assistant-provider-1"].stored === true,
            }));
            """
        ),
    )

    assert output == {
        "messageIds": ["assistant-provider-1", "user-turn-1"],
        "userText": "创建 VPC",
        "assistantText": "完整回复",
        "assistantStored": False,
    }


def test_turn_done_stamps_elapsed_seconds_on_turn_assistant_messages(tmp_path) -> None:
    # 折叠回合头「已处理 <时间>」实时偶尔只显示「已处理」,手动重载又正常:实时助手消息不像
    # 重载转录那样自带 elapsedSeconds,折叠渲染的时长实时只有 state.turns[turnId].elapsedMs
    # 一个来源。resync/重连重建状态后分组 turnId 落不回 state.turns 就丢时长。turn.done 必须把
    # 本轮时长回写到本轮助手消息,让折叠渲染的按消息兜底与重载同源。用户气泡不应被打时长。
    output = _run_events_script(
        tmp_path,
        textwrap.dedent(
            """
            const { reduceEvent } = await import(__EVENTS_MODULE__);

            let state = { messages: {}, tools: {}, turns: {}, lastSequence: 0 };
            state = reduceEvent(state, {
              sequence: 1, type: "user.message",
              payload: { messageId: "user-T", turnId: "T", text: "创建 ECS" },
            });
            state = reduceEvent(state, {
              sequence: 2, type: "assistant.message.start",
              payload: { messageId: "asst-T", turnId: "T" },
            });
            state = reduceEvent(state, {
              sequence: 3, type: "assistant.text.delta",
              payload: { messageId: "asst-T", turnId: "T", delta: "好的" },
            });
            state = reduceEvent(state, {
              sequence: 4, type: "assistant.message.end",
              payload: { messageId: "asst-T", turnId: "T" },
            });
            state = reduceEvent(state, {
              sequence: 5, type: "turn.done",
              payload: { turnId: "T", elapsedMs: 37000 },
            });

            console.log(JSON.stringify({
              turnElapsedMs: state.turns["T"].elapsedMs,
              assistantElapsedSeconds: state.messages["asst-T"].elapsedSeconds ?? null,
              userElapsedSeconds: state.messages["user-T"].elapsedSeconds ?? null,
            }));
            """
        ),
    )

    assert output["turnElapsedMs"] == 37000
    assert output["assistantElapsedSeconds"] == 37.0
    assert output["userElapsedSeconds"] is None


def test_user_message_reducer_clears_prior_turn_error_banner(tmp_path) -> None:
    # provider 切换 bug：某轮失败后 error 事件写进单例 next.lastError,而 app.js 把它当作栈底
    # 唯一错误横幅渲染。旧代码在新一轮 user.message 到来时从不清空 lastError,于是上一轮的报错
    # 一直悬在栈底、随每条新用户气泡「往下移」(无轮次归属),让用户误以为新一轮「没反应」。
    # 现在:新一轮(turnId 不同)的 user.message 会清掉归属其它轮次的历史错误横幅;本轮若再次
    # 失败,error 事件会在其后重新写入 lastError。
    output = _run_events_script(
        tmp_path,
        textwrap.dedent(
            """
            const { reduceEvent } = await import(__EVENTS_MODULE__);

            let state = { messages: {}, tools: {}, turns: {}, lastError: null, lastSequence: 0 };

            // 第 1 轮(QwenPaw,配置无效)——发送 → 失败。
            state = reduceEvent(state, {
              sequence: 1, type: "user.message",
              payload: { messageId: "u1", turnId: "t1", text: "测试一下ls命令" },
            });
            state = reduceEvent(state, {
              sequence: 2, type: "error",
              payload: { message: "PermissionDeniedError: 403 AccessDenied.Unpurchased", turnId: "t1" },
            });
            state = reduceEvent(state, {
              sequence: 3, type: "turn.done",
              payload: { turnId: "t1", failed: true },
            });

            // 第 2 轮(切到百炼)——新一轮 user.message 到来。
            const afterNewTurn = reduceEvent(state, {
              sequence: 4, type: "user.message",
              payload: { messageId: "u2", turnId: "t2", text: "测试一下ls命令" },
            });

            // 本轮自身若再失败,error 应能重新落到 lastError。
            const afterNewError = reduceEvent(afterNewTurn, {
              sequence: 5, type: "error",
              payload: { message: "本轮自己的报错", turnId: "t2" },
            });

            console.log(JSON.stringify({
              lingeringError: afterNewTurn.lastError ? afterNewTurn.lastError.message : null,
              currentTurnActive: afterNewTurn.currentTurnActive === true,
              freshError: afterNewError.lastError ? afterNewError.lastError.message : null,
            }));
            """
        ),
    )

    # 新一轮开始后,上一轮的报错横幅被清掉(不再悬在栈底往下移)。
    assert output["lingeringError"] is None
    # 新一轮仍标记为进行中(用户能看到「正在思考」而非空白「没反应」)。
    assert output["currentTurnActive"] is True
    # 本轮自身的失败仍会正常显示。
    assert output["freshError"] == "本轮自己的报错"


def test_reducer_tracks_and_removes_pipeline_context_window(tmp_path) -> None:
    # pipeline.step.context 事件把某步骤/候选的实时上下文占用登记到 state.activeContextWindows
    # (按 groupId 键);其 groupId 与同作用域 pipeline.step.marker 的 pipelineStep.groupId 一致。
    # 步骤到达终态(completed/failed/canceled/early_exit)的 marker 会删掉对应窗口——供 Task 6 的
    # deriveContextUsageWindows 消费。input(暂停)是非终态,窗口应保留。
    output = _run_events_script(
        tmp_path,
        textwrap.dedent(
            """
            const { reduceEvent } = await import(__EVENTS_MODULE__);

            let state = {};
            state = reduceEvent(state, {
              type: "pipeline.step.context",
              sequence: 1,
              payload: {
                groupId: "step:step-step1-1",
                level: "step",
                stepId: "step1",
                title: "Understand",
                candidateName: "",
                attemptNo: 1,
                contextUsage: { totalTokens: 1500, contextWindow: 60000 },
              },
            });
            const afterContext = state.activeContextWindows["step:step-step1-1"]?.contextUsage?.totalTokens;
            state = reduceEvent(state, {
              type: "pipeline.step.marker",
              sequence: 2,
              payload: {
                markerId: "plmk-step-step1-1",
                kind: "pipeline_step",
                content: "● Understand",
                pipelineStep: { groupId: "step:step-step1-1", status: "completed" },
              },
            });
            const afterComplete = state.activeContextWindows["step:step-step1-1"];
            console.log(JSON.stringify({ afterContext, afterComplete: afterComplete ?? null }));
            """
        ),
    )

    assert output == {"afterContext": 1500, "afterComplete": None}


def test_reducer_keeps_window_across_compaction_boundary(tmp_path) -> None:
    # 问题 #1/#4：压缩边界 marker(kind="context_compaction_boundary")复用候选 live groupId
    # 且 status="completed",但候选并未结束——压缩只是它中途一步。reducer 不得按终态删窗,
    # 否则圆环在压缩期间凭空消失(多候选圈数 2→1;单候选悬浮显示「普通会话」)。
    output = _run_events_script(
        tmp_path,
        textwrap.dedent(
            """
            const { reduceEvent } = await import(__EVENTS_MODULE__);

            let state = {};
            state = reduceEvent(state, {
              type: "pipeline.step.context",
              sequence: 1,
              payload: {
                groupId: "candidate:cand-1",
                level: "candidate",
                title: "Generate",
                candidateName: "方案 A",
                contextUsage: { totalTokens: 32000, contextWindow: 60000 },
              },
            });
            state = reduceEvent(state, {
              type: "pipeline.step.marker",
              sequence: 2,
              payload: {
                markerId: "plmk-compact-1",
                kind: "context_compaction_boundary",
                content: "● 压缩上下文",
                pipelineStep: { groupId: "candidate:cand-1", status: "completed" },
              },
            });
            const afterCompaction = state.activeContextWindows["candidate:cand-1"] ?? null;
            console.log(JSON.stringify({
              kept: afterCompaction !== null,
              total: afterCompaction?.contextUsage?.totalTokens ?? null,
            }));
            """
        ),
    )

    assert output == {"kept": True, "total": 32000}


def test_render_messages_orders_by_transcript_sequence_not_stored_flag() -> None:
    # Issue 7：renderMessages 排序须以 sequence 为主键。同进程 reload 里流水线步骤内容会被缓冲区
    # 回放的 assistant.message.start 翻成 stored=false——若以 stored 为主键，这些内容会被甩到所有
    # stored 行之后（步骤体清空、内容错位到底部）。sequence 为主、stored 仅作次键才不受翻转影响。
    source = _source(APP_JS)

    assert "Number.isFinite(message.sequence) && message.sequence > 0" in source
    assert "Number.POSITIVE_INFINITY" in source
    assert "const orderRank = (message) =>" in source
    # stored 仅作次键（同序号时历史排在新流式消息前）。
    assert "(left.stored ? 0 : 1) - (right.stored ? 0 : 1)" in source


def test_session_switch_is_optimistic_with_loading_animation() -> None:
    # 会话切换应「先切后加载」：点击时同步高亮目标会话、清空正文并显示加载动画，
    # 再异步拉取正文，避免在 loadSession 的串行请求返回前界面毫无反应（不丝滑）。
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # switchSession 在 await loadSession 之前先做一次乐观状态切换并 render。
    assert "function findSessionSummary(" in app_source
    assert "loadingSession: true" in app_source
    # 乐观 render 必须发生在 await loadSession 之前（限定在 switchSession 函数体内，
    # 因为 handleStreamEvent 的 resync 也会 await loadSession，会先命中）。
    switch_body = app_source[app_source.index("async function switchSession(") :]
    optimistic = switch_body.index("loadingSession: true")
    bind_composer = switch_body.index("composer?.setSession(sessionId)")
    optimistic_render = switch_body.index("render(state)")
    await_load = switch_body.index("await loadSession(sessionId, { forceDraft: false, generation })")
    assert optimistic < await_load
    # 输入控制器必须在乐观渲染前同步改绑到目标会话。否则加载窗口里允许输入时，
    # composer 仍持有旧 session id，会把消息投递给刚切走的会话。
    assert optimistic < bind_composer < optimistic_render < await_load
    # 异常时清掉加载态，避免转圈永久卡死。
    assert "loadingSession: false" in switch_body

    # renderMessages 在加载态且正文为空时渲染加载动画而非「开始构建」引导块。
    assert "state.loadingSession && messages.length === 0" in app_source
    assert "message-loading" in app_source
    assert "Loading session" in app_source

    # 加载动画样式与转圈动画存在。
    assert ".message-loading" in styles
    assert ".message-loading-spinner" in styles
    assert "animation: iac-thread-spin" in styles
    # 加载态相对「输入框」左右居中(而非整页):复刻 .composer/.message-empty 的靠左锚定 40rem 跨度,
    # 再用 flex justify-content:center 把内容居中于该盒子,居中点与输入框中线重合。
    loading_block = styles.split(".message-loading {", 1)[1].split("}", 1)[0]
    assert "justify-content: center;" in loading_block
    assert "width: min(40rem, calc(100% - 2rem));" in loading_block
    assert "margin-left: 0;" in loading_block


def test_sidebar_renders_unread_dot_for_finished_unviewed_sessions() -> None:
    # 进行中的会话结束、且未查看时,侧边栏该行右侧显示未读蓝点(仿 Codex)。
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # createThreadRow 依据 session.unread + 非当前行 + 非进行中/等待 计算是否显示圆点。
    assert "const showUnread = Boolean(session.unread)" in app_source
    assert 'activity === ""' in app_source
    assert "session-unread-dot" in app_source
    # 未读时以圆点替代时间戳(二者同占第 3 列):meta 在 showUnread 时隐藏。
    assert "if (!metaText || showUnread)" in app_source

    # 圆点样式:落在第 3 列、蓝点、悬停让位给操作按钮。
    assert ".session-unread-dot" in styles
    assert "--codex-unread" in styles
    assert ".thread-item:hover .session-unread-dot" in styles


def test_api_registers_named_sse_listeners_for_every_backend_event() -> None:
    source = _source(API_JS)

    assert "WEB_EVENT_TYPES" in source
    assert "source.addEventListener(eventType" in source
    for event_type in REQUIRED_WEB_EVENT_TYPES:
        assert event_type in source


def test_api_stream_uses_session_events_endpoint_with_replay_cursor() -> None:
    source = _source(API_JS)

    assert "/api/sessions/" in source
    assert "/events" in source
    assert "afterSequence" in source
    assert "EventSource" in source
    assert ".onmessage" not in source


def test_api_stream_disconnect_is_transient_and_clears_after_reconnect(tmp_path: Path) -> None:
    output = _run_api_script(
        tmp_path,
        textwrap.dedent(
            """
            const events = [];
            class FakeEventSource {
              constructor(url) {
                this.url = url;
                this.listeners = new Map();
                globalThis.lastSource = this;
              }
              addEventListener(type, handler) { this.listeners.set(type, handler); }
              removeEventListener(type) { this.listeners.delete(type); }
              close() {}
            }
            globalThis.window = { location: { origin: "http://127.0.0.1:8766" } };
            globalThis.EventSource = FakeEventSource;

            const { openEventStream } = await import(__API_MODULE__);
            const { reduceEvent } = await import(__EVENTS_MODULE__);
            let state = { lastError: { message: "real turn failure" } };
            openEventStream("session-1", 0, (event) => {
              events.push(event.type);
              state = reduceEvent(state, event);
            });

            globalThis.lastSource.onerror();
            await Promise.resolve();
            const disconnected = {
              lastError: state.lastError?.message || "",
              streamConnectionError: state.streamConnectionError?.message || "",
            };
            globalThis.lastSource.onopen();
            await Promise.resolve();

            console.log(JSON.stringify({
              events,
              disconnected,
              reconnected: {
                lastError: state.lastError?.message || "",
                streamConnectionError: state.streamConnectionError?.message || "",
              },
            }));
            """
        ),
    )

    assert output == {
        "events": ["stream.disconnected", "stream.connected"],
        "disconnected": {
            "lastError": "real turn failure",
            "streamConnectionError": "Event stream disconnected",
        },
        "reconnected": {
            "lastError": "real turn failure",
            "streamConnectionError": "",
        },
    }


def test_api_exposes_pipeline_state_hydration_endpoint() -> None:
    source = _source(API_JS)

    assert "getPipelineState" in source
    assert "/api/pipeline/state" in source
    assert "contextId" in source
    assert "taskId" in source
    assert "afterSequence" in source


def test_api_exposes_pipeline_candidate_selection_endpoint() -> None:
    source = _source(API_JS)

    assert "export function selectPipelineCandidate" in source
    assert '"/api/pipeline/candidates/select"' in source
    for key in ["sessionId", "candidateName", "candidateIndex", "parameterOverrides"]:
        assert key in source


def test_i18n_runtime_present() -> None:
    i18n = I18N_JS.read_text(encoding="utf-8")
    assert "export function t(" in i18n
    assert "export function applyDomI18n(" in i18n
    assert "window.__IAC_I18N__" in i18n
    app = APP_JS.read_text(encoding="utf-8")
    assert "./i18n.js?v=" in app
    assert "applyDomI18n(document)" in app


def test_app_wires_named_stream_reducer_blocking_tools_and_composer() -> None:
    source = _source(APP_JS)

    assert "openEventStream" in source
    assert "reduceEvent" in source
    assert "renderBlockingPanels" in source
    assert "renderToolCards" in source
    assert "createComposerController" in source
    assert "pendingPermissions" in source
    assert "pendingQuestions" in source
    assert "connectCurrentStream" in source
    assert "session.resync.required" in source
    assert "replaySequence" in source
    assert "await api.getMessages(sessionId)" in source
    assert "dedupeReplayMessages" in source
    assert "left.stored ? 0 : 1" in source
    assert "messageText(candidate).trim() === content" not in source
    assert 'event.type === "draft.updated"' in source
    assert "force: false" in source
    assert "renderPipelineWorkspace" in source
    assert "orderedUserInputs" in source
    assert "composer?.setInputHistory(orderedUserInputs(messages))" in source
    assert 'byShell("pipeline-workspace")' in source
    assert "await api.getPipelineState" in source
    assert "pipelineSnapshot: pipelineState.snapshot" in source
    assert "pipelineEvents: pipelineState.events" in source
    assert "candidateDetails: pipelineState.snapshot?.display?.candidateDetails" in source
    assert "diagrams: pipelineState.snapshot?.display?.diagrams" in source
    assert "pipelineError" in source
    assert "pipelineDisplayReplay" in source
    assert "lastError" in source
    assert "message-error" in source
    assert "COMMAND_PALETTE_ITEMS" in source
    assert "openCommandPalette" in source
    assert "refreshPalette" in source
    assert 'event.key.toLowerCase() === "k"' in source
    assert "renderQueuedInputs" in source
    assert 'byShell("queued-inputs")' in source
    assert "throw error" not in source


def test_pipeline_replay_details_are_structured_not_raw_json() -> None:
    source = _source(PIPELINE_JS)
    styles = _source(STYLES_CSS)

    assert "pipeline-replay-detail-metrics" in source
    assert "appendReplayMetric" in source
    assert "appendReplayList" in source
    assert "JSON.stringify(attempt" not in source
    assert ".pipeline-replay-detail-metrics" in styles
    assert ".pipeline-replay-detail-list" in styles


def test_app_renders_replayed_markdown_thinking_and_attached_tools() -> None:
    html = _source(INDEX_HTML)
    source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    assert "/static/js/vendor/markdown-it.min.js" in html
    assert "window.markdownit" in source
    assert "renderMarkdownInto" in source
    assert "markdown-body" in source
    assert "message-thinking" in source
    assert 'document.createElement("details")' in source
    assert "message.thinking" in source
    # 思考折叠块必须挂稳定展开态键,否则 toggle 记录器 return、每帧重建回落收起→"思考中无法展开"。
    assert 'thinking.dataset.openKey = `think:${text(message.messageId || message.id || "")}`;' in source
    assert "toolUseIds" in source
    assert "storedMessages.tools" in source
    assert "tools: storedTools" in source
    assert "renderToolCards(messageToolState, {" in source
    assert "turnActive: !!state.currentTurnActive," in source

    for snippet in [
        ".message-thinking",
        ".message-thinking summary",
        ".markdown-body",
        ".markdown-body pre",
        ".markdown-body code",
        ".markdown-body table",
        ".markdown-body ul",
    ]:
        assert snippet in styles


def test_transcript_chat_flow_uses_codex_unboxed_message_layout() -> None:
    styles = _source(STYLES_CSS)

    label_block = _css_block(styles, ".transcript-panel .message-label")
    user_block = _css_block(styles, ".transcript-panel .message-user")
    agent_block = _css_block(styles, ".transcript-panel .message-agent")
    agent_toolcards_block = _css_block(styles, ".message-agent:has(.message-tool-cards)")
    agent_body_block = _css_block(styles, ".message-agent:has(.message-tool-cards) > .message-body")
    tool_container_block = _css_block(styles, ".message-tool-cards .tool-group,\n.message-tool-cards .tool-card")
    tool_row_block = _css_block(styles, ".message-tool-cards .tool-group-summary,\n.message-tool-cards .tool-card-row")
    tool_icon_block = _css_block(styles, ".message-tool-cards .tool-group-icon,\n.message-tool-cards .tool-card-icon")
    tool_icon_before_block = _css_block(
        styles, ".message-tool-cards .tool-group-icon::before,\n.message-tool-cards .tool-card-icon::before"
    )
    tool_icon_after_block = _css_block(
        styles, ".message-tool-cards .tool-group-icon::after,\n.message-tool-cards .tool-card-icon::after"
    )
    group_list_block = _css_block(styles, ".message-tool-cards .tool-group-list")
    thinking_summary_block = _css_block(styles, ".message-thinking summary")
    thinking_icon_block = _css_block(styles, ".message-thinking summary::before")
    thinking_marker_block = _css_block(styles, ".message-thinking summary::-webkit-details-marker")

    assert "display: none;" in label_block
    assert "margin-right: max(0rem, calc(100% - 40rem));" in user_block
    assert "padding: 0;" in agent_block
    assert "border: 0;" in agent_block
    assert "background: transparent;" in agent_block
    assert "padding: 0;" in agent_body_block
    assert "border: 0;" in agent_body_block
    assert "border-radius: 0;" in agent_body_block
    assert "background: transparent;" in agent_body_block
    # 带工具卡的 agent 网格必须约束单列为 minmax(0, 1fr),否则隐式 max-content 列会把子项撑到 ~40rem,
    # 在被缩进的流水线步骤体里整列右缘溢出到步骤边界外。
    assert "grid-template-columns: minmax(0, 1fr);" in agent_toolcards_block
    assert "padding: 0;" in tool_container_block
    assert "margin: 0;" in tool_container_block
    assert "padding: 0.06rem 0;" in tool_row_block
    assert "grid-template-columns: 0.68rem minmax(0, 1fr) auto auto;" in tool_row_block
    assert "gap: 0.14rem;" in tool_row_block
    assert 'content: ">";' in tool_icon_before_block
    assert 'content: "_";' in tool_icon_after_block
    assert "font-family:" in tool_icon_block
    assert "padding-left: 0;" in group_list_block
    assert "display: grid;" in thinking_summary_block
    # 图标列宽/间距与工具行一致,灯泡才不会比下方工具图标多缩进一截。
    assert "grid-template-columns: 0.68rem minmax(0, 1fr) auto;" in thinking_summary_block
    assert "gap: 0.14rem;" in thinking_summary_block
    assert "list-style: none;" in thinking_summary_block
    # 思考图标为灯泡(mask 描边式,跟随 currentColor),取代旧的圆圈+box-shadow 图形。
    assert "mask:" in thinking_icon_block
    assert "--iac-thinking-bulb:" in thinking_icon_block
    assert "display: none;" in thinking_marker_block


def test_transcript_renders_pipeline_step_markers_as_section_rows() -> None:
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    for snippet in [
        'kind: typeof message.kind === "string" ? message.kind : ""',
        'pipelineStep: message.pipelineStep && typeof message.pipelineStep === "object" ? message.pipelineStep : null',
        "function renderPipelineMarkerGroup(message, options = {})",
        "function renderPipelineBoundaryMarker(message)",
        "const pipelineStack = [{ depth: -1, body: stack }];",
        'if (message.kind === "normal_chat_boundary")',
        'message.kind === "pipeline_step"',
        'message.kind === "pipeline_candidate"',
        'message.kind === "pipeline_sub_step"',
        # 进行中的步骤展开、终态（completed / canceled）后自动收起（对齐 normal 轮次）；
        # 等待输入（status==="input"）的步骤强制展开，绝不收起（Issue 1）。
        'const awaitingInput = status === "input";',
        'details.open = status === "working" || status === "" || awaitingInput;',
        'details.dataset.forceOpen = "1";',
        # applyDetailsOpenOverrides 跳过 forceOpen 步骤，保证等待输入时不被折叠。
        'if (details.dataset.forceOpen === "1") {',
        # 等待输入时显式「等待输入」文案 + 转圈，明确不是卡死（Issue 1）。
        'hint.className = "pipeline-step-input-hint";',
        'hint.textContent = t("Waiting for input");',
        # 稳定键（markerId）让用户手动展开态跨帧重建保留（Issue 3/5）。
        'details.dataset.openKey = `mk:${text(message.messageId || message.id || "")}`;',
        # 进行中显示转圈特效，结束显示「已处理 <时长>」。
        'spinner.className = "thread-spinner pipeline-step-spinner";',
        't("Processed {elapsed}", { elapsed })',
        # 分组内逐段渲染时隐藏重复的「IaC Code」标签，并把转录尾部最新工具卡透传下去（Issue 3）。
        "renderConversationMessage(message, state, { hideLabel: true, openToolUseId: latestToolUseId }),",
        "details.className = `message-pipeline-step pipeline-transcript-group ${groupClass}`;",
        "while (pipelineStack.length > 1 && pipelineStack[pipelineStack.length - 1].depth >= depth)",
    ]:
        assert snippet in app_source

    for snippet in [
        ".message-pipeline-step",
        ".pipeline-transcript-group",
        ".pipeline-step-summary",
        ".pipeline-step-icon",
        ".pipeline-candidate-group",
        ".pipeline-sub-step-group",
        ".pipeline-normal-boundary",
        ".pipeline-step-spinner",
        # 等待输入提示的样式（Issue 1）。
        ".pipeline-step-input-hint",
    ]:
        assert snippet in styles


def test_current_thread_header_supports_codex_style_rename_menu() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    for snippet in [
        'class="thread-current"',
        'data-app-shell="thread-title"',
        'data-app-shell="thread-menu-toggle"',
        'data-app-shell="thread-menu"',
        'data-app-shell="thread-pin"',
        'data-app-shell="thread-rename"',
        'data-app-shell="thread-archive"',
        'data-app-shell="app-modal"',
        'data-app-shell="app-modal-form"',
        'data-app-shell="app-modal-input"',
        'data-app-shell="app-modal-textarea"',
        'data-app-shell="app-modal-error"',
        "Pin conversation",
        "Rename conversation",
        "Archive conversation",
    ]:
        assert snippet in html

    for snippet in [
        "renderThreadHeader",
        "openThreadMenu",
        "closeThreadMenu",
        "startThreadRename",
        "toggleCurrentSessionPinned",
        "archiveCurrentSession",
        "openAppModal",
        "submitAppModal",
        "setAppModalError",
        "api.updateSession",
        "try {",
        "catch (error)",
        'byShell("thread-menu-toggle")?.addEventListener("click"',
        'byShell("thread-rename")?.addEventListener("click"',
        'byShell("thread-pin")?.addEventListener("click"',
        'byShell("thread-archive")?.addEventListener("click"',
        'byShell("app-modal-form")?.addEventListener("submit"',
    ]:
        assert snippet in app_source

    for selector in [
        ".thread-current",
        ".thread-title-button",
        ".thread-menu-button",
        ".thread-menu",
        ".thread-menu-item",
        ".thread-menu-icon-pin",
        ".thread-menu-icon-archive",
        ".app-modal",
        ".app-modal-form",
        ".app-modal-error",
    ]:
        assert selector in styles


def test_thread_menu_anchors_directly_under_toggle_button() -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)

    # The toggle button and its popover share a dedicated relative anchor so the
    # menu opens directly under the "…" button rather than under the title text.
    assert 'class="thread-menu-anchor"' in html
    anchor_block = _css_block(styles, ".thread-menu-anchor")
    assert "position: relative;" in anchor_block

    menu_block = _css_block(styles, ".thread-menu")
    assert "position: absolute;" in menu_block
    assert "left: 0;" in menu_block


def test_mobile_thread_title_keeps_a_positive_readable_width() -> None:
    styles = _source(STYLES_CSS)

    assert "max-width: min(28rem, calc(100vw - 8rem));" in styles
    assert "min-width: 3rem;" in styles
    assert "flex: 1 1 auto;" in styles


def test_project_section_header_has_collapse_controls() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # Real DOM header replaces the former ::before "项目" pseudo-label.
    for snippet in [
        'data-app-shell="project-nav-header"',
        'data-app-shell="projects-section-toggle"',
        'data-app-shell="projects-collapse-all"',
        'class="project-nav-chevron"',
        'class="project-nav-collapse-icon"',
    ]:
        assert snippet in html

    for snippet in [
        "projectsSectionCollapsed",
        "toggleProjectsSectionCollapsed",
        "toggleAllProjectsCollapsed",
        "updateProjectNavHeader",
        'byShell("projects-section-toggle")?.addEventListener("click"',
        'byShell("projects-collapse-all")?.addEventListener("click"',
    ]:
        assert snippet in app_source

    for selector in [
        ".project-nav-header",
        ".project-nav-toggle",
        ".project-nav-chevron",
        ".project-nav-collapse-all",
        ".project-nav-collapse-icon",
    ]:
        assert selector in styles

    # The former pseudo-element label must no longer inject "项目" text.
    assert '.project-thread-nav::before {\n  content: "项目";' not in styles

    # Section chevron sits to the RIGHT of the "项目" title, not the left.
    assert html.index('class="project-nav-title"') < html.index('class="project-nav-chevron"')

    # Both header controls reveal on hover of the whole header row, not the
    # individual buttons, and hide when the pointer leaves the row.
    chevron_block = _css_block(styles, ".project-nav-chevron")
    assert "opacity: 0;" in chevron_block
    assert ".project-nav-header:hover .project-nav-chevron" in styles
    assert '.project-nav-toggle[aria-expanded="false"] .project-nav-chevron' in styles
    collapse_all_block = _css_block(styles, ".project-nav-collapse-all")
    assert "opacity: 0;" in collapse_all_block
    assert ".project-nav-header:hover .project-nav-collapse-all" in styles

    # Per-project collapse chevron sits directly after the name label (revealed
    # on row hover), not grouped with the far-right menu/new-thread actions.
    assert "projectName.append(projectIcon, projectLabel, projectCollapse)" in app_source
    assert "projectActions.append(projectMenu, projectNewThread)" in app_source
    assert "projectActions.append(projectCollapse" not in app_source
    assert "projectRow.append(projectName, projectCount, projectActions)" in app_source
    assert ".project-name-label" in styles
    assert ".project-row:hover .project-collapse" in styles
    # The leading left chevron grid column (1.2rem) is gone; row starts with name.
    assert "grid-template-columns: minmax(0, 1fr) auto auto;" in styles
    assert "grid-template-columns: 1.2rem minmax(0, 1fr) auto auto;" not in styles


def test_context_compaction_indicator_replicates_codex_running_state() -> None:
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # app.js: 只在压缩进行中(status==="running")渲染指示器,自动/手动文案区分。
    assert "function buildCompactionIndicator(" in app_source
    assert 'state.compaction?.status === "running"' in app_source
    assert "Auto-compacting context" in app_source
    assert "Compacting context" in app_source
    assert 'wrap.className = "context-compaction is-compacting"' in app_source

    # styles.css: 顶部分隔线容器 + 文案复用流光(iac-shimmer-sweep)选择器组。
    assert ".context-compaction {" in styles
    assert ".context-compaction.is-compacting .context-compaction-label" in styles

    # 进行中指示器左对齐消息正文左缘(justify-content: flex-start),并把分隔线宽度收到 composer
    # 同款上限(#38:此前 width:100% 让 border-top 横贯 820px 整段、溢出更窄的输入框)。
    compaction_block = styles.split(".context-compaction {", 1)[1].split("}", 1)[0]
    assert "justify-content: flex-start" in compaction_block
    assert "margin: 0.85rem 0 0" in compaction_block
    assert "max-width: min(40rem, 100%)" in compaction_block
    assert "max-width: 46rem" not in compaction_block


def test_compaction_finished_surfaces_outcome_not_blank() -> None:
    # Bug A: 动画结束后不能一片空白。成功→重载出持久「上文已压缩」分隔条;
    # 未产生新边界(too_short/empty/failed/blocked)→行内提示告知结果。
    app_source = _source(APP_JS)
    events_source = _source(EVENTS_JS)
    styles = _source(STYLES_CSS)

    # 成功:handleStreamEvent 拦截 compaction.finished + state==="success",走 loadSession 重载。
    assert 'event.type === "compaction.finished" && event.payload?.state === "success"' in app_source
    # 幂等守卫:context_id 会话重连会从缓冲区底重放 compaction.finished(success),重放事件不能再重载,
    # 否则压缩成功→重载→重连→重放→再重载死循环。按 {会话 id, 事件序号} 只重载一次。
    assert "lastReloadedCompaction" in app_source
    assert "const alreadyReloaded =" in app_source
    # 未成功:render 分支 + buildCompactionNotice + 文案表。
    assert 'state.compaction?.status === "completed"' in app_source
    assert "function buildCompactionNotice(" in app_source
    assert "COMPACTION_NOTICE_TEXT" in app_source
    assert "The conversation is short; no context compaction needed yet." in app_source
    assert "Context compaction failed. Please try again later." in app_source
    # 新一轮开始清掉一次性提示,避免它一直悬在栈底。
    assert 'if (next.compaction?.status === "completed") {' in events_source
    # 提示样式:与压缩边界同宽居中、muted。
    assert ".context-compaction-notice {" in styles


def test_compact_command_does_not_open_deprecated_status_modal() -> None:
    # 手动 /compact(实时结果只带 command:"compact",无 action)与自动压缩都不应再
    # 路由到废弃的「状态」面板——压缩进度由内联指示器 + compaction SSE 呈现。
    app_source = _source(APP_JS)

    # commandWorkspaceTab 的命令路由白名单不含 compact;action 路由白名单不含 compact_session。
    assert '["help", "prompt", "rename", "clear", "debug"].includes(command)' in app_source
    assert "compact_session" not in app_source
    # compact 不出现在任何「打开模态」的正向白名单里(help/prompt/rename/clear/debug),
    # 且被显式排除在通用 accepted===false 兜底之外——失败/过短的压缩只走内联提示,不弹「状态」模态。
    assert 'command !== "compact"' in app_source


def test_mcp_command_opens_inline_status_panel() -> None:
    # /mcp 与 /status 一样是即时命令,回车/点击后弹出独立的只读 MCP 信息面板,
    # 而非打开原有的 MCP 设置模态(设置面板保持不变)。
    composer_source = _source(COMPOSER_JS)
    app_source = _source(APP_JS)
    index_html = _source(INDEX_HTML)
    styles_source = _source(STYLES_CSS)

    # composer: mcp 进入即时执行 + 会话内命令集,并有展示 token/描述/图标。
    assert 'mcp: "MCP"' in composer_source
    assert 'mcp: t("Show MCP server status")' in composer_source
    assert 'mcp: "is-command-mcp"' in composer_source
    assert 'new Set(["status", "compact", "mcp"])' in composer_source

    # app.js: 命令结果分流到内联 MCP 面板;render 每帧渲染该面板。
    assert 'commandResult.command === "mcp"' in app_source
    assert "showInlineMcpStatus(commandResult.mcp || {})" in app_source
    assert "renderInlineMcpStatusPanel(state)" in app_source
    # 与「状态」面板互斥:显示其一时清空另一。
    assert "inlineMcpStatus: null" in app_source

    # shell 存在;CSS 卡片与命令图标存在。
    assert 'data-app-shell="mcp-status-panel"' in index_html
    assert ".mcp-status-card" in styles_source
    assert ".suggestion-icon.is-command-mcp::before" in styles_source


def test_composer_queues_input_while_compacting() -> None:
    # 压缩/自动压缩进行中,提交必须进入排队(等压缩完成),而不是新起 turn。
    composer_source = _source(COMPOSER_JS)
    app_source = _source(APP_JS)

    # composer: 新增 compacting 标志与 setCompacting,提交分支按 turnActive||compacting 排队。
    assert "let compacting = false;" in composer_source
    assert "setCompacting(active)" in composer_source
    assert "if (turnActive || compacting) {" in composer_source

    # app.js: 每次 render 依据 compaction.status 同步 compacting 态。
    assert 'composer?.setCompacting(state.compaction?.status === "running")' in app_source


def test_static_asset_versions_reload_rename_api_changes() -> None:
    html = _source(INDEX_HTML)
    index_html = html
    app_source = _source(APP_JS)
    workspace_source = _source(WORKSPACE_JS)

    assert "/static/styles.css?v=web-repl-ui-313" in html
    assert "/static/js/app.js?v=web-repl-ui-321" in html
    # api.js 导出 WEB_EVENT_TYPES(EventSource 订阅白名单)与 openEventStream;新增
    # pipeline.step.marker 订阅后必须 bump 其 import 版本位,否则回访浏览器加载「新
    # app.js + 旧缓存 api.js」,EventSource 仍不监听该事件名,实时流水线主区照样空白。
    # 已归档面板复刻(archived tab)新增 listArchivedSessions/deleteArchivedSessions,
    # 同样需 bump api.js 版本位,否则回访浏览器拿不到新导出。
    assert "./api.js?v=web-repl-ui-307" in app_source
    assert "./components/composer.js?v=session-model-v19" in app_source
    # 图片灯箱模块(composer 缩略图 + 消息内图片共用),改动需 bump 其 import 版本位。
    assert "./components/image_lightbox.js?v=image-lightbox-v1" in app_source
    assert "./components/tool_cards.js?v=live-inline-tools-v23" in app_source
    assert "./components/blocking.js?v=blocking-keys-v5" in app_source
    # events.js 承载队列/消息 reducer,历次修复都在此;它的 import 必须带版本位,
    # 否则回访浏览器会加载「新 app.js + 旧缓存 events.js」,让队列行为与当前代码不一致。
    assert 'from "./events.js?v=' in app_source

    # 流水线 ask_user_question 的答案必须走标准 pipeline 消息通道(postMessage),
    # 不能走 answerQuestion(该暂停点未在 question manager 注册)。
    assert "state.questions?.[requestId]?.payload?.pipeline" in app_source
    assert "await api.postMessage(sessionId, { text: message })" in app_source

    # 上下文圆环须在 turn 进行中刷新:handleStreamEvent 收到附带 contextUsage 的流事件
    # (assistant.message.end / turn.done)后,把它写回当前会话,下一帧 renderStatus 驱动圆环。
    assert "event.payload?.contextUsage" in app_source
    assert "contextUsage: liveContextUsage" in app_source

    # 多圆环:renderStatus 依据 state.activeContextWindows(Task 4)派生活动窗口,
    # 经 deriveContextUsageWindows 排序后喂给 composer.setContextUsages(Task 5)。
    assert "deriveContextUsageWindows" in app_source
    assert "setContextUsages(" in app_source
    assert "activeContextWindows" in app_source

    # cloud-creds 面板(Task 5/6)重写后须 bump 全局版本位并给 workspace.js 加 per-file
    # 版本位,否则回访浏览器加载旧缓存 workspace.js,拿不到新的云凭证面板结构。
    assert "web-repl-ui-321" in index_html
    # events.js 新增实时 MCP/工具进度归并，必须 bump 版本避免旧 reducer 丢事件。
    assert "./events.js?v=web-repl-ui-319" in app_source
    assert "./components/workspace.js?v=cloud-creds-v51" in app_source
    assert "workspace-cloud-vendors" in workspace_source
    assert "Alibaba Cloud" in workspace_source
    assert "workspace-cloud-mode-fields" in workspace_source
    assert "workspace-cloud-oauth-login" in workspace_source
    assert "ALIYUN_REGIONS" in workspace_source
    assert "East China 2 (Shanghai)" in workspace_source
    assert "OAuth browser login" in workspace_source
    # OAuth 等待可取消(修复关闭浏览器后界面卡死):取消按钮 marker 必须在源码中。
    assert "workspace-cloud-oauth-cancel" in workspace_source
    # 区域字段改为强制组合框以支持手填不在列表内的地域;地域列表扩充到完整 ECS 地域。
    assert "forceFree: true" in workspace_source
    assert "ap-northeast-1" in workspace_source
    # 区域下拉改为自定义常显菜单(原生 datalist 会按输入过滤,换 region 不便):
    # 自定义控件保留 <input> 但菜单恒列全部地域,故须有对应构造器与类名。
    assert "makeFreeDropdown" in workspace_source
    assert "workspace-choice-menu" in workspace_source
    assert "workspace-choice-toggle" in workspace_source
    # OAuth 登录后展示访问令牌过期时间(本地时区),并在 OAuth 模式隐藏「保存云凭证」底部按钮。
    assert "formatLocalEpoch" in workspace_source
    assert "Access token expiry" in workspace_source
    assert "actions.hidden" in workspace_source
    # 「加载凭证」按钮已移除:进入面板即自动加载当前选中凭证,该按钮无额外作用。
    assert "加载凭证" not in workspace_source
    assert "workspace-cloud-load" not in workspace_source
    # StsExpiration 由后端自动派生,不再让用户填写:移除输入框与渲染行。
    assert "workspace-cloud-sts-expiration" not in workspace_source
    # OAuth 已登录即展示刷新令牌一行;阿里云不返回有效期时给出诚实兜底文案而非隐藏。
    assert "Refresh token expiry" in workspace_source
    assert "Unknown (Alibaba Cloud did not provide an expiry)" in workspace_source
    # OAuth 派生的 STS 临时凭证独立展示第三行;尚未换取时给出诚实兜底文案。
    assert "STS expiry" in workspace_source
    assert "Unknown (STS credentials not yet obtained)" in workspace_source


def test_token_mode_frontend_uses_only_encrypted_transport_for_business_data() -> None:
    api_source = _source(API_JS)
    transport_source = _source(TOKEN_TRANSPORT_JS)
    crypto_source = _source(TOKEN_CRYPTO_JS)
    app_source = _source(APP_JS)
    composer_source = _source(COMPOSER_JS)

    assert 'from "./token_transport.js?v=token-transport-v3"' in api_source
    assert 'fetch("/api/token/challenge"' in transport_source
    assert 'fetch("/api/token/ping"' in transport_source
    assert 'stream ? "/api/token/stream" : "/api/token/request"' in transport_source
    assert "window.sessionStorage" in transport_source
    assert "document.cookie" not in transport_source
    assert "crypto.subtle" not in crypto_source
    assert "https://" not in crypto_source
    assert 'jsonFetch("/api/cloud/aliyun/oauth-start"' in api_source
    assert 'jsonFetch("/api/cloud/aliyun/oauth-complete"' in api_source
    assert "authorizationWindow?.close()" in api_source
    assert "authorizationWindow.location.href" not in api_source
    assert "await requestAuthorizationCode({ signal })" in api_source
    assert "window.prompt" not in transport_source
    assert "oauth-code-input" in transport_source
    assert "new AbortController()" in _source(WORKSPACE_JS)
    assert "oauthAbortController?.abort()" in _source(WORKSPACE_JS)
    token_dialog = _css_block(_source(STYLES_CSS), ".token-access-dialog")
    token_input = _css_block(_source(STYLES_CSS), ".token-access-input")
    assert "background: var(--codex-panel-raised)" in token_dialog
    assert "color: var(--codex-text)" in token_dialog
    assert "var(--codex-ink)" in token_input
    assert "color: var(--codex-text)" in token_input
    assert "getImageObjectUrl" in api_source
    assert "api.getImageObjectUrl" in app_source
    assert "api.getImageObjectUrl" in composer_source


def test_token_crypto_matches_python_interop_vectors(tmp_path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "token-vector.mjs"
    script.write_text(
        textwrap.dedent(
            f"""
            import {{
              chacha20poly1305Encrypt,
              hkdfSha256,
              base64UrlEncode,
            }} from {json.dumps(TOKEN_CRYPTO_JS.as_uri())};
            const encoder = new TextEncoder();
            const key = Uint8Array.from({{length: 32}}, (_, index) => index);
            const nonce = Uint8Array.from({{length: 12}}, (_, index) => index + 32);
            const encrypted = chacha20poly1305Encrypt(
              key,
              nonce,
              encoder.encode("hello token transport"),
              encoder.encode("iac-code-aad"),
            );
            const derived = hkdfSha256(
              encoder.encode("token"),
              encoder.encode("salt"),
              encoder.encode("iac-code-web-token-v1"),
              64,
            );
            const key2 = Uint8Array.from({{length: 32}}, (_, index) => 255 - index);
            const nonce2 = Uint8Array.from({{length: 12}}, (_, index) => (17 * index) % 256);
            const encrypted2 = chacha20poly1305Encrypt(
              key2,
              nonce2,
              encoder.encode("ping"),
              encoder.encode("v1\\nsession-two\\nrequest\\nping\\n1"),
            );
            const derived2 = hkdfSha256(
              encoder.encode("another-token"),
              Uint8Array.from({{length: 32}}, (_, index) => index + 1),
              encoder.encode("iac-code-web-token-v1"),
              64,
            );
            process.stdout.write(JSON.stringify({{
              encrypted: base64UrlEncode(encrypted),
              derived: base64UrlEncode(derived),
              encrypted2: base64UrlEncode(encrypted2),
              derived2: base64UrlEncode(derived2),
            }}));
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "encrypted": "G4LkEH7PiFXwoMPoPU3u1ggeEUBkDj8CLtQzy7fkTnk5vChXpA",
        "derived": "VpWNFUgiXfWtSb9zSF-saEQCsGKv40UX_ujm-fikLPsyKAEi39LxCQG3ABR6bJFnLLFnbtsg9Ib5LgaQ8eA9wA",
        "encrypted2": "JpOtxAXrIdk7XH1JRyBKvTSt-Fc",
        "derived2": "QzDXpz7rGU8wM98Vb3SS590czheQ3t9XMFWwss4mQWzcBvOjZXknBKeGBKZwu_rXoT-5EdhwmSFVjM4VGm_1sQ",
    }


def test_token_transport_rebuilds_bodyless_response(tmp_path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "token-bodyless-response.mjs"
    script.write_text(
        textwrap.dedent(
            f"""
            import {{
              base64UrlEncode,
              chacha20poly1305Encrypt,
              concatBytes,
              hkdfSha256,
            }} from {json.dumps(TOKEN_CRYPTO_JS.as_uri())};

            const encoder = new TextEncoder();
            const token = base64UrlEncode(Uint8Array.from({{length: 32}}, (_, index) => index));
            const salt = Uint8Array.from({{length: 32}}, (_, index) => index + 32);
            const responsePrefix = encoder.encode("resp");
            const sessionId = "bodyless-session";
            const keys = hkdfSha256(
              encoder.encode(token),
              salt,
              encoder.encode("iac-code-web-token-v1"),
              64,
            );
            const responseKey = keys.slice(32);
            const storage = new Map([["iac-code:web-access-token", token]]);
            globalThis.window = {{
              __IAC_I18N__: {{ lang: "en", messages: {{}} }},
              location: new URL("http://203.0.113.10:8766/"),
              sessionStorage: {{
                getItem: (key) => storage.get(key) || null,
                setItem: (key, value) => storage.set(key, value),
                removeItem: (key) => storage.delete(key),
              }},
            }};
            globalThis.document = {{ body: {{ dataset: {{ tokenMode: "true" }} }} }};

            function envelope(type, sequence, plaintext) {{
              const counter = new Uint8Array(8);
              new DataView(counter.buffer).setBigUint64(0, BigInt(sequence));
              const aad = encoder.encode(`v1\\n${{sessionId}}\\nresponse\\n${{type}}\\n${{sequence}}`);
              return {{
                sessionId,
                sequence,
                type,
                ciphertext: base64UrlEncode(chacha20poly1305Encrypt(
                  responseKey,
                  concatBytes(responsePrefix, counter),
                  encoder.encode(plaintext),
                  aad,
                )),
              }};
            }}

            const challenge = {{
              version: "v1",
              sessionId,
              salt: base64UrlEncode(salt),
              requestNoncePrefix: base64UrlEncode(encoder.encode("reqp")),
              responseNoncePrefix: base64UrlEncode(responsePrefix),
              expiresAt: Math.floor(Date.now() / 1000) + 300,
            }};
            globalThis.fetch = async (url) => {{
              if (url === "/api/token/challenge") return Response.json(challenge);
              if (url === "/api/token/ping") return Response.json(envelope("pong", 1, "pong"));
              if (url === "/api/token/request") {{
                return Response.json(envelope("response", 2, JSON.stringify({{
                  status: 204,
                  headers: [],
                  body: "",
                }})));
              }}
              throw new Error(`unexpected fetch: ${{url}}`);
            }};

            const {{ tokenFetch }} = await import({json.dumps(TOKEN_TRANSPORT_JS.as_uri())});
            const response = await tokenFetch("/api/update/dismiss", {{ method: "POST" }});
            process.stdout.write(JSON.stringify({{ status: response.status, body: await response.text() }}));
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": 204, "body": ""}


def test_oauth_manual_code_uses_inline_dialog_and_supports_abort(tmp_path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "oauth-code-dialog.mjs"
    script.write_text(
        textwrap.dedent(
            f"""
            class Element {{
              constructor() {{
                this.listeners = {{}};
                this.value = "";
                this.textContent = "";
                this.placeholder = "";
                this.removed = false;
              }}
              addEventListener(type, callback) {{
                this.listeners[type] = callback;
              }}
              dispatch(type, event = {{}}) {{
                this.listeners[type]?.(event);
              }}
              focus() {{}}
              remove() {{ this.removed = true; }}
            }}

            function createGate() {{
              const gate = new Element();
              const selectors = new Map([
                ["h1", new Element()],
                [".oauth-code-description", new Element()],
                [".oauth-code-input", new Element()],
                [".oauth-code-error", new Element()],
                [".oauth-code-cancel", new Element()],
                [".oauth-code-submit", new Element()],
                ["form", new Element()],
              ]);
              gate.querySelector = (selector) => selectors.get(selector);
              return gate;
            }}

            let activeGate = null;
            globalThis.window = {{
              __IAC_I18N__: {{ lang: "en", messages: {{}} }},
              prompt() {{ throw new Error("native prompt must not be used"); }},
            }};
            globalThis.document = {{
              body: {{ append(gate) {{ activeGate = gate; }} }},
              createElement() {{ return createGate(); }},
            }};

            const {{ requestAuthorizationCode }} = await import({json.dumps(TOKEN_TRANSPORT_JS.as_uri())});
            const submittedPromise = requestAuthorizationCode();
            const submittedGate = activeGate;
            submittedGate.querySelector(".oauth-code-input").value =
              "http://127.0.0.1:12345/cli/callback?code=example&state=expected";
            submittedGate.querySelector("form").dispatch("submit", {{ preventDefault() {{}} }});
            const submitted = await submittedPromise;

            const abortController = new AbortController();
            const abortedPromise = requestAuthorizationCode({{ signal: abortController.signal }});
            const abortedGate = activeGate;
            abortController.abort();
            let abortName = "";
            try {{
              await abortedPromise;
            }} catch (error) {{
              abortName = error.name;
            }}
            process.stdout.write(JSON.stringify({{
              submitted,
              submittedRemoved: submittedGate.removed,
              abortName,
              abortedRemoved: abortedGate.removed,
            }}));
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "submitted": "http://127.0.0.1:12345/cli/callback?code=example&state=expected",
        "submittedRemoved": True,
        "abortName": "AbortError",
        "abortedRemoved": True,
    }


def test_archived_conversations_panel_replicates_codex_management_view() -> None:
    # 「已归档对话」面板复刻 Codex 归档管理界面:顶栏标题 + 「全部删除」、筛选栏
    # (搜索框 + 类型/排序下拉 + 项目下拉)、按项目分组、每组「…」菜单、悬停显露
    # 「取消归档」与垃圾桶。前端结构、API 客户端、CSS 与静态壳(tab/panel)缺一不可。
    html = _source(INDEX_HTML)
    workspace_source = _source(WORKSPACE_JS)
    api_source = _source(API_JS)
    styles = _source(STYLES_CSS)

    # 设置弹窗新增「历史」分组下的「已归档对话」tab 与对应面板占位。
    assert "workspace-tab-icon-archived" in html
    assert 'data-workspace-tab="archived"' in html
    assert 'data-workspace-panel="archived"' in html
    assert "Archived conversations" in html

    # API 客户端暴露归档列举/删除端点。
    assert "export function listArchivedSessions" in api_source
    assert "export function deleteArchivedSessions" in api_source
    assert '"/api/sessions/archived"' in api_source

    # 面板工厂 + 类型/排序、项目下拉构造器,并挂到 archived 面板控制器上。
    for snippet in [
        "function createArchivedPanel",
        'panelControllers.set("archived", createArchivedPanel(api, context))',
        "const ARCHIVED_TYPE_LABELS = { all: ",
        "const ARCHIVED_SORT_LABELS = { updated: ",
        "function makeArchivedTypeSortDropdown",
        "function makeArchivedProjectDropdown",
        "function sortArchivedSessions",
        "api.listArchivedSessions()",
        "api.deleteArchivedSessions(group.cwd)",
        "api.deleteArchivedSessions()",
        # 取消归档 = PATCH archived:false,复用既有 updateSession。
        "api.updateSession(archivedSessionId(session), { archived: false })",
        # 分组头/项目下拉用侧边栏同款短标签(重名逐级消歧),不展示全路径。
        "function archivedProjectDisplayLabels",
        "const displayLabels = archivedProjectDisplayLabels(allProjects.map((group) => group.cwd))",
        # Codex 同款前置图标:搜索框包裹层、全部聊天=漏斗、所有项目=文件夹。
        'className: "workspace-archived-search-wrap"',
        "workspace-archived-dropdown-trigger--filter",
        "workspace-archived-dropdown-trigger--project",
    ]:
        assert snippet in workspace_source

    # 界面文案与 Codex 归档界面一致。
    for label in ["全部删除", "取消归档", "删除项目中的全部内容", "全部聊天"]:
        assert label in workspace_source

    # 本应用无「本地/云端」「聊天/已安排任务」这些桶,对应下拉项已移除。
    # 注:归档类型标签精简为仅「全部聊天」,以此对象字面量为准,避免与其它面板
    # (如 MCP 作用域标签复用「本地」一词)产生误伤式全局子串匹配。
    assert 'const ARCHIVED_TYPE_LABELS = { all: t("All chats") };' in workspace_source
    assert 'cloud: "云端"' not in workspace_source
    assert 'makeArchivedDropdownOption("聊天"' not in workspace_source
    assert 'makeArchivedDropdownOption("已安排任务"' not in workspace_source

    # 归档面板每次激活都重新拉取,刚归档的会话无需刷新页面即出现。
    assert "每次切到该标签都重新拉取" in workspace_source

    # 条目时间精确到分钟(YYYY年M月D日,HH:MM),并单独占第二排。
    assert "pad(date.getHours())" in workspace_source
    assert "pad(date.getMinutes())" in workspace_source
    info_block = _css_block(styles, ".workspace-archived-item-info")
    assert "flex-direction: column;" in info_block

    # 「删除项目中的全部内容」补 Codex 同款垃圾桶图标(::before,确认态改文案不丢)。
    assert ".workspace-archived-group-menu-item.is-danger::before" in styles

    # 面板专属样式(顶栏、筛选下拉、分组、条目悬停操作)。
    for selector in [
        ".workspace-tab-icon-archived",
        ".workspace-archived-panel",
        ".workspace-archived-delete-all",
        ".workspace-archived-dropdown",
        ".workspace-archived-dropdown-menu",
        ".workspace-archived-groups",
        ".workspace-archived-group",
        ".workspace-archived-group-head",
        ".workspace-archived-group-count",
        ".workspace-archived-item",
        ".workspace-archived-item-actions",
        ".workspace-archived-item-trash",
        ".workspace-archived-item-unarchive",
        ".workspace-archived-status",
        # Codex 同款图标:全部删除垃圾桶、搜索放大镜、下拉漏斗/文件夹、分组头文件夹。
        ".workspace-archived-delete-all::before",
        ".workspace-archived-search-wrap::before",
        ".workspace-archived-dropdown-trigger--filter",
        ".workspace-archived-dropdown-trigger--project",
        ".workspace-archived-group-icon",
    ]:
        assert selector in styles

    # 悬停才显露条目操作(默认透明,hover 可见),对齐 Codex 交互。
    actions_block = _css_block(styles, ".workspace-archived-item-actions")
    assert "opacity: 0;" in actions_block
    assert ".workspace-archived-item:hover .workspace-archived-item-actions" in styles


def test_pipeline_transcript_is_event_driven_not_polled() -> None:
    app_source = _source(APP_JS)
    events_source = _source(EVENTS_JS)

    # 流水线主区改为事件驱动:后端翻译器把 A2A 信封转成 pipeline.step.marker 事件,
    # reducer 直接灌进 state.messages,复用恢复态嵌套渲染——不再有轮询定时器。
    assert 'case "pipeline.step.marker":' in events_source
    assert "message.pipelineStep = payload.pipelineStep" in events_source
    # 失效的 display.jsonl 轮询路径必须彻底移除。
    assert "pollPipelineTranscript" not in app_source
    assert "startPipelinePolling" not in app_source
    assert "stopPipelinePolling" not in app_source
    # buildStoredTranscript 仍用于普通会话加载路径。
    assert "function buildStoredTranscript(" in app_source
    # 主区不得再内联 renderPipelineWorkspace(诊断网格)。
    assert "appendPipelineWorkspace" not in app_source


def test_session_list_shows_running_spinner_and_awaiting_pill() -> None:
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # 活动状态判定：等待批准优先于进行中，当前会话读实时 state，其它会话读快照字段。
    assert "function sessionActivityState(session, state" in app_source
    assert 'return "awaiting"' in app_source
    assert 'return "running"' in app_source
    assert "state.currentTurnActive" in app_source
    assert "session.pendingPermissionCount" in app_source

    # 行渲染：进行中转圈，等待批准显示绿色提示。
    assert '"thread-status"' in app_source
    assert '"thread-spinner"' in app_source
    assert '"thread-status-pill"' in app_source
    assert "Awaiting approval" in app_source

    # 样式：转圈动画 + hover 时让位给操作按钮。
    spinner_block = _css_block(styles, ".thread-spinner")
    assert "animation: iac-thread-spin" in spinner_block
    assert "iac-thread-spin" in styles
    assert ".thread-status" in styles

    # 转圈相位对齐：列表每次 replaceChildren 重建都重造 spinner <span>，若不对齐相位则动画从 0° 重启
    # （用户见「转着转着被拽回原点」）。新建 spinner 后须调 applySpinPhase 续到 performance.now() 相位。
    assert "applySpinPhase" in app_source
    assert "applySpinPhase(spinner, 1.4)" in app_source


def test_sidebar_running_state_stays_fresh_after_switch_and_polling() -> None:
    app_source = _source(APP_JS)

    # 非当前会话的转圈/时间只来自列表快照,需要一个只重绘侧边栏的刷新助手。
    assert "async function refreshSessionsSidebar()" in app_source
    assert "renderSessions(state)" in app_source

    # 切换会话后重新拉取快照,原本正在运行的会话切走后仍显示转圈。
    switch_region = app_source.split("async function switchSession(")[-1].split("\nasync function")[0]
    assert "refreshSessionsSidebar()" in switch_region

    # 定时后台刷新,让运行状态随后台轮次开始/结束出现或消失、相对时间保持新鲜。
    assert "function startSessionsAutoRefresh()" in app_source
    assert "document.hidden" in app_source
    assert "startSessionsAutoRefresh();" in app_source

    # 自适应轮询:列表里有「非当前会话」在进行中/等待时提速,把「结束→转圈残留/变未读」
    # 的可见延迟从 12s 慢周期压到 2.5s 快周期,避免驱动页与旁观页长时间显示矛盾状态。
    assert "const SESSIONS_REFRESH_ACTIVE_MS = 2500;" in app_source
    assert "function sidebarHasBackgroundActivity()" in app_source
    assert "sidebarHasBackgroundActivity() ? SESSIONS_REFRESH_ACTIVE_MS : SESSIONS_REFRESH_INTERVAL_MS" in app_source
    # 自重排的 setTimeout(await 完成后再排下一次)避免慢接口下的请求叠加。
    assert "setTimeout(runSessionsRefreshTick" in app_source
    # 后台标签页重新可见时立即补刷一次,切回原页面无需再等一个周期。
    assert 'addEventListener("visibilitychange"' in app_source


def test_sidebar_defers_repaint_while_pointer_inside() -> None:
    app_source = _source(APP_JS)

    # 运行中侧栏(流式逐帧 render + 后台 2.5s 轮询)高频整栏 replaceChildren 重建,销毁光标下的行,
    # 令 :hover 背景与 hover 才显形的操作按钮反复通断——用户反馈的「一闪闪」。指针在侧栏内时挂起
    # 这类自动重绘(只记账不重建),pointerleave 追平一次。
    assert "let sidebarPointerInside = false;" in app_source
    assert "let sidebarRepaintPending = false;" in app_source

    assert "function renderSessionsAuto(state)" in app_source
    auto_body = app_source.split("function renderSessionsAuto(state)", 1)[1].split("\n}", 1)[0]
    assert "if (sidebarPointerInside)" in auto_body
    assert "sidebarRepaintPending = true;" in auto_body
    assert "renderSessions(state);" in auto_body

    # 流式 render 与后台刷新都改走节流包装,不再直接 renderSessions。
    assert "renderSessionsAuto(state);" in app_source
    render_body = app_source.split("function render(state)", 1)[1].split("\n}", 1)[0]
    assert "renderSessionsAuto(state);" in render_body
    refresh_body = app_source.split("async function refreshSessionsSidebar()", 1)[1].split("\n}", 1)[0]
    assert "renderSessionsAuto(state);" in refresh_body

    # 稳定容器 .session-rail 绑定进出边界探测(只一次);容器不被 replaceChildren 重建,监听长期有效。
    assert "function ensureSidebarHoverGuard()" in app_source
    guard_body = app_source.split("function ensureSidebarHoverGuard()", 1)[1].split("\n}\n", 1)[0]
    assert ".session-rail" in guard_body
    assert 'addEventListener("pointerenter"' in guard_body
    assert 'addEventListener("pointerleave"' in guard_body
    # pointerleave 时若有挂起重绘则追平一次。
    assert "sidebarRepaintPending" in guard_body
    assert "ensureSidebarHoverGuard();" in app_source


def test_inline_status_panels_skip_rebuild_when_unchanged() -> None:
    app_source = _source(APP_JS)

    # 会话进行中,流式逐帧 render 反复调用两个内联状态面板的渲染;面板内容是打开时的快照,期间不变,
    # 但旧实现每帧都 replaceChildren 重建,销毁并重建光标下的关闭按钮,令其 :hover 反复通断
    # (用户反馈鼠标悬停「一闪闪」)。内容签名一致时短路,完全不动 DOM,且短路必须发生在
    # replaceChildren 之前。
    status_body = app_source.split("function renderInlineSessionStatusPanel(currentState = {})", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert 'const signature = status ? JSON.stringify(rows) : "";' in status_body
    assert "if (target.dataset.statusSignature === signature) {" in status_body
    assert "target.dataset.statusSignature = signature;" in status_body
    assert status_body.index("dataset.statusSignature === signature") < status_body.index("replaceChildren()")

    mcp_body = app_source.split("function renderInlineMcpStatusPanel(currentState = {})", 1)[1].split("\n}\n", 1)[0]
    assert 'const signature = mcp ? JSON.stringify(servers) : "";' in mcp_body
    assert "if (target.dataset.mcpStatusSignature === signature) {" in mcp_body
    assert "target.dataset.mcpStatusSignature = signature;" in mcp_body
    assert mcp_body.index("dataset.mcpStatusSignature === signature") < mcp_body.index("replaceChildren()")


def test_complete_step_tool_renders_conclusion_card() -> None:
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")

    # complete_step gets its own detection + card renderer, not the raw-JSON generic path.
    assert "export function isCompleteStepTool" in tool_cards
    assert 'lowerToolName(tool) === "complete_step"' in tool_cards
    assert "function renderCompleteStepDetail" in tool_cards
    assert "function renderConclusionValue" in tool_cards
    assert "function completeStepConclusion" in tool_cards

    # The card title reflects the completed step, and the detail reads the nested conclusion.
    assert "Completed step" in tool_cards
    assert '"conclusion"' in tool_cards

    # Sub-pipeline step conclusions (template / review / cost / deploy) get field labels
    # instead of leaking raw English keys.
    for label in (
        "Template",
        "Review passed",
        "InfraGuard summary",
        "Total violations",
        "Deployment parameters",
        "Resource inventory",
        "Region",
    ):
        assert label in tool_cards

    # Deploy-result and candidate-confirmation steps also get labels.
    for label in (
        "Resources created",
        "Outputs",
        "User prompt",
        "Options",
        "Selected candidate name",
        "Missing deployment parameters",
    ):
        assert label in tool_cards

    # renderToolCard must branch to the complete_step renderer before the shell/generic paths.
    card_body = tool_cards.split("function renderToolCard", 1)[1]
    assert "if (isCompleteStepTool(tool)) {" in card_body
    assert card_body.index("isCompleteStepTool(tool)") < card_body.index("isShellTool(tool)")

    # Conclusion tree is styled (not dumped as a <pre> JSON blob).
    styles = _source(STYLES_CSS)
    assert ".tool-complete-step-detail" in styles
    assert ".conclusion-object" in styles
    assert ".conclusion-field" in styles


def test_named_tools_get_chinese_action_labels() -> None:
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")

    # Known tool identifiers map to full action phrases, consulted before the
    # substring read/write/list heuristics (so read_memory is not labeled "Read file").
    assert "const TOOL_ACTION_LABELS" in tool_cards
    for name, label in (
        ("read_memory", "Read memory"),
        ("write_memory", "Saved memory"),
        ("infraguard_scan", "Ran InfraGuard scan"),
        ("show_architecture_diagram", "Showed architecture diagram"),
        ("show_candidate_detail", "Showed candidate details"),
        ("ros_stack", "Operated ROS stack"),
        # Pipeline ROS tools split out from the monolithic ros_stack.
        ("ros_validate_template", "Validated template"),
        ("ros_get_template_parameter_constraints", "Fetched template parameter constraints"),
        ("ros_preview_template", "Previewed stack changes"),
        ("ros_estimate_template_cost", "Estimated resource cost"),
        ("ros_deploy", "Deployed stack"),
        # aliyun_api_doc(阿里云 API 文档查询)独立于 aliyun_doc_search,需专属短语。
        ("aliyun_api_doc", "Looked up Alibaba Cloud API reference"),
    ):
        assert f"{name}:" in tool_cards
        assert label in tool_cards

    # The map lookup must run before the isShellTool/isReadTool branches when building
    # the done-state (past tense) label.
    command_body = tool_cards.split("function toolPhrase", 1)[1]
    assert "TOOL_ACTION_LABELS[name]" in command_body
    assert command_body.index("TOOL_ACTION_LABELS[name]") < command_body.index("isShellTool(tool)")


def test_mcp_tools_render_server_dot_tool_label_and_icon() -> None:
    # MCP 工具命名形如 mcp__server__tool。此前 web 落到通用卡:标题直接暴露原始长命名、
    # 无 server 归属、无专用图标。B3 要求识别 mcp__ 前缀,解析出 server / tool 两段,
    # 渲染成「server · tool」标签并给专属 MCP 图标(卡片 data-tool-kind="mcp")。
    tool_cards = _source(TOOL_CARDS_JS)
    # 识别与解析函数。
    assert "function isMcpTool" in tool_cards
    assert '"mcp__"' in tool_cards or "'mcp__'" in tool_cards
    assert "function mcpToolLabel" in tool_cards
    # 标签用「 · 」连接 server 与 tool 两段。
    label_body = tool_cards.split("function mcpToolLabel", 1)[1].split("\n}", 1)[0]
    assert " · " in label_body

    # toolPhrase 必须在通用回退之前走 MCP 专属分支(复用 Calling/Called/Call failed 短语)。
    phrase_body = tool_cards.split("function toolPhrase", 1)[1].split("\nexport function", 1)[0]
    assert "isMcpTool(tool)" in phrase_body
    assert "mcpToolLabel(tool)" in phrase_body
    # MCP 分支必须先于「Using {name}」通用兜底。
    assert phrase_body.index("isMcpTool(tool)") < phrase_body.index('t("Using {name}"')

    # canceled/denied 保留目标时也要走 MCP 标签。
    target_body = tool_cards.split("function toolActionTarget", 1)[1].split("\n}", 1)[0]
    assert "isMcpTool(tool)" in target_body

    # 卡片挂 data-tool-kind="mcp",供 CSS 换用 MCP 图标。
    card_body = tool_cards.split("function renderToolCard", 1)[1]
    assert 'toolKind = "mcp"' in card_body or "dataset.toolKind" in card_body

    # 样式:MCP 卡的图标字形区别于通用 >_。
    styles = _source(Path(__file__).parents[2] / "src/iac_code/web/static/styles.css")
    assert 'tool-card[data-tool-kind="mcp"]' in styles


def test_effort_label_covers_minimal_and_none() -> None:
    # DashScope glm-5.2 / Gemini-3 等模型的 effort 档含 minimal / none;此前 effortLabel
    # 只映射 low/medium/high/xhigh/max/auto,这两档在 UI 上显示原始小写英文。B4 要求补齐
    # 本地化标签。
    composer = _source(COMPOSER_JS)
    label_body = composer.split("function effortLabel", 1)[1].split("\n}", 1)[0]
    assert "minimal: t(" in label_body
    assert "none: t(" in label_body
    assert 't("Minimal")' in label_body
    assert 't("None")' in label_body


def test_pipeline_candidate_panel_strings_are_localized() -> None:
    # B5:workspace 候选详情面板(renderCandidateActions / renderCandidates)此前硬编码
    # 英文字面量,未走 t()。要求这些用户可见文案全部经过 t() 本地化。
    pipeline = _source(PIPELINE_JS)
    for label in (
        "Parameter overrides",
        "Select candidate",
        "Selected",
        "Submitting...",
        "Candidates",
        "No candidates.",
    ):
        assert f't("{label}")' in pipeline, f"candidate panel string not localized: {label}"
    # 不得残留裸字面量赋值(容易在重构时回退)。
    assert 'textContent = "Parameter overrides"' not in pipeline
    assert 'textContent = "No candidates."' not in pipeline
    assert 'textContent = "Submitting..."' not in pipeline


def test_pipeline_tool_json_result_gets_structured_labels() -> None:
    # Pipeline 工具(如 ros_deploy)的结果串是后端 json.dumps 的原始 JSON,存在 results[].content。
    # 通用工具详情必须先尝试把它解析为对象,命中则交给 renderConclusionValue 做带中文字段标签的
    # 结构化渲染(复用 CONCLUSION_LABELS),而不是原封不动塞进 <pre>。解析失败(纯文本)才回退 <pre>。
    tool_cards = _source(TOOL_CARDS_JS)
    assert "function structuredResultValue" in tool_cards
    # 结果分支必须先取结构化值,命中才走 renderConclusionValue,否则回退 appendValueBlock。
    results_branch = tool_cards.split("if (options.includeResults !== false) {", 1)[1].split("\n  }", 1)[0]
    assert "structuredResultValue(tool)" in results_branch
    assert "renderConclusionValue(structured)" in results_branch
    assert 'appendValueBlock(detail, "tool-card-results", t("Result"), tool.results)' in results_branch
    # 新增字段的标签必须落在 CONCLUSION_LABELS 里。
    for key, label in (
        ("stack_name", "Stack name"),
        ("stack_id", "Stack ID"),
        ("status_reason", "Status reason"),
        ("is_success", "Is success"),
        ("recommended_action", "Recommended action"),
        ("selected_label", "Selected item"),
        ("free_text", "Free text"),
        ("ResourceTypes", "Resource types"),
        ("ParameterConstraints", "Parameter constraints"),
    ):
        assert f"{key}:" in tool_cards
        assert label in tool_cards
    # 字段标签经 t() 包裹以支持 i18n。
    assert 'stack_id: t("Stack ID")' in tool_cards
    # ROS 资源栈状态码作为结果里的「值」出现,须按整串精确匹配翻译。
    assert "const CONCLUSION_VALUE_LABELS" in tool_cards
    for code, label in (
        ("CREATE_COMPLETE", "Create complete"),
        ("CREATE_FAILED", "Create failed"),
        ("ROLLBACK_COMPLETE", "Rollback complete"),
    ):
        assert f"{code}:" in tool_cards
        assert label in tool_cards
    # conclusionScalarText 必须在回退到原始文本前查一次状态码映射。
    scalar_body = tool_cards.split("function conclusionScalarText", 1)[1].split("\n}", 1)[0]
    assert "CONCLUSION_VALUE_LABELS" in scalar_body
    assert scalar_body.index("CONCLUSION_VALUE_LABELS") < scalar_body.index("return text(value)")


def test_pipeline_tool_json_result_renders_labeled_fields(tmp_path) -> None:
    # 端到端:用最小 DOM 垫片跑真实 renderToolCards。ros_deploy 的 JSON 结果应渲染成带中文标签
    # 的结构化列表(资源栈名称/状态原因/是否成功…);纯文本结果的工具仍以原始文本呈现。
    output = _run_toolcards_script(
        tmp_path,
        textwrap.dedent(
            """
            class Element {
              constructor(tag) {
                this.tagName = tag;
                this.className = "";
                this.dataset = {};
                this.style = {};
                this.children = [];
                this.open = false;
                this._text = "";
              }
              set textContent(value) { this._text = value == null ? "" : String(value); }
              get textContent() { return this._text; }
              append(...nodes) {
                for (const node of nodes) {
                  if (node && typeof node === "object") { this.children.push(node); }
                }
              }
            }
            globalThis.document = { createElement: (tag) => new Element(tag) };

            const { renderToolCards } = await import(__TOOLCARDS_MODULE__);

            function textOf(node) {
              const own = node._text || "";
              const kids = (node.children || []).map(textOf).join(" ");
              return `${own} ${kids}`.replace(/\\s+/g, " ").trim();
            }

            const deployState = {
              tools: {
                t1: {
                  toolUseId: "t1",
                  toolName: "ros_deploy",
                  status: "completed",
                  results: [
                    {
                      content: JSON.stringify({
                        stack_name: "my-stack",
                        status: "CREATE_COMPLETE",
                        status_reason: "Stack CREATE completed",
                        is_success: true,
                        progress_percentage: 100,
                      }),
                    },
                  ],
                },
              },
            };
            const plainState = {
              tools: {
                t2: {
                  toolUseId: "t2",
                  toolName: "ros_deploy",
                  status: "completed",
                  results: [{ content: "deploy finished, no json here" }],
                },
              },
            };

            const deployText = textOf(renderToolCards(deployState, { turnActive: false }));
            const plainText = textOf(renderToolCards(plainState, { turnActive: false }));
            console.log(JSON.stringify({ deployText, plainText }));
            """
        ),
    )

    deploy_text = output["deployText"]
    # 已知字段套标签,值一并呈现;布尔渲染为「Yes」。
    assert "Stack name" in deploy_text
    assert "my-stack" in deploy_text
    assert "Status reason" in deploy_text
    assert "Stack CREATE completed" in deploy_text
    assert "Is success" in deploy_text
    # 状态码作为值须翻译,不再暴露英文 CREATE_COMPLETE。
    assert "Create complete" in deploy_text
    assert "CREATE_COMPLETE" not in deploy_text
    # 未命中 CONCLUSION_LABELS 的原始 snake_case key 不应直接出现(已被标签/人性化取代)。
    assert "status_reason" not in deploy_text
    assert "stack_name" not in deploy_text
    # 纯文本结果解析不出对象,回退到原始文本呈现。
    assert "deploy finished, no json here" in output["plainText"]


def test_expanding_tool_card_rechecks_message_stack_overflow() -> None:
    # Issue #1: expanding a <details> tool card must re-evaluate message-stack overflow so the
    # page stays scrollable. The stack uses align-content:end, which hides overflow above the
    # top; a capture-phase toggle listener flips it to align-content:start when content overflows.
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    assert "function ensureMessageStackToggleSync" in app_source
    assert "function refreshMessageStackOverflow" in app_source
    # toggle does not bubble, so the listener must be registered in the capture phase.
    toggle_body = app_source.split("function ensureMessageStackToggleSync", 1)[1]
    assert '"toggle"' in toggle_body
    assert "HTMLDetailsElement" in toggle_body
    assert "refreshMessageStackOverflow(stack)" in toggle_body
    # capture phase: the addEventListener call ends with `true` before the closing paren.
    assert "    true,\n  );" in toggle_body
    # The listener must be wired up when messages render.
    assert "ensureMessageStackToggleSync(stack)" in app_source

    # The overflow class actually switches alignment so the overflow becomes reachable/scrollable.
    overflowing_block = _css_block(styles, ".transcript-panel .message-stack.is-overflowing")
    assert "align-content: start;" in overflowing_block


def test_transcript_message_stack_pins_single_grid_track() -> None:
    # Issue 5: the transcript's implicit auto grid column sized to the widest item's
    # max-content (a long paragraph / table / unbreakable string can blow it far past
    # the 820px container), leaving a phantom empty vertical strip on the right while
    # the inner content still wrapped narrower. Pin the single track to minmax(0, 1fr)
    # and zero the children's min-width so they can only shrink to wrap within it.
    styles = _source(STYLES_CSS)
    stack_block = _css_block(styles, ".transcript-panel .message-stack")
    assert "grid-template-columns: minmax(0, 1fr);" in stack_block
    child_block = _css_block(styles, ".transcript-panel .message-stack > *")
    assert "min-width: 0;" in child_block


def test_failed_tool_card_is_styled_as_error() -> None:
    # Issue 4: a failed tool must look failed, not merely「已完成」。The is-error card's
    # title turns danger-red/bold and its icon strokes recolor to danger.
    styles = _source(STYLES_CSS)
    title_block = _css_block(styles, ".message-tool-cards .tool-card.is-error > .tool-card-row .tool-card-title")
    assert "color: var(--danger);" in title_block
    assert "font-weight: 600;" in title_block
    assert ".message-tool-cards .tool-card.is-error > .tool-card-row .tool-card-icon::before," in styles


def test_expand_state_persists_across_full_rebuild() -> None:
    # Issue 2/3/5: renderMessages full-rebuilds the stack every frame, so a <details>
    # the user expanded got reset next frame. A stable-id override store records the
    # user's intent (via click, not the programmatic default) and reapplies it after
    # rebuild; running tool cards/groups default to open so 「正在XX」 is visible.
    app_source = _source(APP_JS)
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")

    # A module-level override store + apply/clear helpers.
    assert "const detailsOpenOverrides = new Map();" in app_source
    assert "function applyDetailsOpenOverrides(stack)" in app_source
    assert "function clearDetailsOpenOverrides()" in app_source
    # User clicks (not programmatic .open writes) record the post-toggle state by key.
    # Must be recorded SYNCHRONOUSLY as !details.open (predicted post-toggle): the click always
    # toggles the <summary>'s <details>, and stream renders share the same rAF queue
    # (scheduleStreamRender) — a deferred (rAF) record loses the race, rebuilding the group
    # collapsed with no follow-up render to reapply the intent (点击展开无效). See ensureMessageStackToggleSync.
    assert "detailsOpenOverrides.set(details.dataset.openKey, !details.open)" in app_source
    # 记录不再延到 rAF（曾是竞态根因）。
    assert "requestAnimationFrame(record)" not in app_source
    assert 'summary = event.target?.closest?.("summary")' in app_source
    assert "details.dataset.openKey" in app_source
    # The overrides are reapplied at the end of renderMessages, after the rebuild.
    assert "applyDetailsOpenOverrides(stack);" in app_source
    # Switching to a different session clears the store (avoid markerId collisions);
    # a same-session resync keeps it so an in-progress expand survives the reload.
    assert "clearDetailsOpenOverrides();" in app_source
    assert "previousSessionId && previousSessionId !== state.currentSessionId" in app_source

    # Tool cards + groups carry stable open keys and default running ones to open;
    # the transcript-tail latest card/group also stays open until the next
    # message/tool arrives, so a fast tool no longer just「闪一下」（Issue 3）。
    assert "card.dataset.openKey = `tool:${text(tool.toolUseId)}`;" in tool_cards
    assert (
        "const isLatest = Boolean(options.openToolUseId) && text(tool.toolUseId) === options.openToolUseId;"
        in tool_cards
    )
    # 展开决策抽成纯函数 shouldOpenToolCard;流水线(collapseNonComplete)下全部收起(含 complete_step),
    # 非流水线维持「complete_step/进行中/最新展开」。
    assert "export function shouldOpenToolCard(" in tool_cards
    assert "card.open = shouldOpenToolCard({" in tool_cards
    assert "group.dataset.openKey = `grp:${text(tools[0]?.toolUseId)}`;" in tool_cards
    assert (
        "const holdsLatest = Boolean(openToolUseId) && tools.some((tool) => text(tool.toolUseId) === openToolUseId);"
        in tool_cards
    )
    # 工具组展开态统一由 groupActive || holdsLatest 驱动(回合进行中与静息态同一套规则):运行中/
    # 刚追加时展开;组内工具跑完且助手已产出正文→holdsLatest 转假→自动收起。仅流水线转录
    # (collapseNonComplete)强制收起。组内每张卡仍由 shouldOpenToolCard 保持收起。
    group_body = tool_cards.split("function renderToolGroup(", 1)[1].split("\n}\n", 1)[0]
    assert "if (collapseNonComplete) {" in group_body
    assert "group.open = false;" in group_body
    assert "group.open = groupActive || holdsLatest;" in group_body
    # 旧的「进行中一律收起」单行判定已移除。
    assert "group.open = collapseNonComplete || turnActive ? false : groupActive || holdsLatest;" not in tool_cards
    # 旧的「turnActive 一律强制展开」分支已移除(否则所有工具跑完后工具组仍展开、无法自动收起)。
    assert "if (turnActive) {" not in group_body
    # 助手已产出正文时,转录不再把该消息的工具视为"最新",工具组随即收起。
    latest_body = app_source.split("function latestToolUseIdForTranscript(", 1)[1].split("\n}\n", 1)[0]
    assert "if (messageText(message)) {" in latest_body
    # 思考(含进行中的"正在思考")与正文/流水线标记一样是非工具边界:工具组全部跑完后转入思考即应收起,
    # 不必等思考完成。仅对"无工具的后续消息"生效(本条自带工具时上方 toolId 分支已优先返回)。
    assert "text(message.thinking)" in latest_body
    # 组内卡片渲染必须转发 collapseNonComplete/turnActive:否则 shouldOpenToolCard 走默认
    # (turnActive=false)分支,让 openToolUseId 命中的尾部卡片展开——正是「运行中组里最后一个
    # 工具仍展开」的成因。缺失任一实参都会退回旧行为。
    assert "renderToolCard(tool, { openToolUseId, collapseNonComplete, turnActive })" in group_body
    # The transcript tail's latest tool id is computed and threaded into the render.
    assert "function latestToolUseIdForTranscript" in app_source
    assert "openToolUseId: latestToolUseId" in app_source


def test_streaming_updates_do_not_yank_scroll_or_rebuild_per_event() -> None:
    # 大会话在轮次运行时的两个卡顿源：(1) 每次渲染都强制滚到底，用户上翻看历史会被拽回；
    # (2) 每个 SSE 事件都全量重建整段正文。下面断言 sticky-bottom 与合并渲染两处修复都在源码里。
    app_source = _source(APP_JS)

    # (1) sticky-bottom：渲染前记录是否贴底，只有贴底/首屏/切换会话才滚到底。
    assert "function isMessageStackNearBottom" in app_source
    assert "stack.dataset.stickBottom" in app_source
    sync_body = app_source.split("function syncMessageStackOverflow", 1)[1].split("\nfunction ", 1)[0]
    assert 'stack.dataset.stickBottom !== "0"' in sync_body
    # 切换 / 重载会话强制落到最新消息。
    assert "pendingScrollToBottom = true;" in app_source

    # (2) 合并渲染：流式事件走 rAF 合并，而不是逐事件直接 render(state)。
    assert "function scheduleStreamRender" in app_source
    stream_body = app_source.split("async function handleStreamEvent", 1)[1].split("\nfunction ", 1)[0]
    assert "scheduleStreamRender();" in stream_body
    assert "render(state);" not in stream_body


def test_permission_panel_fallback_strings_are_chinese() -> None:
    # Issue #4: the authorization panel must read entirely in Chinese, including the fallbacks
    # produced by the frontend and the labels/message generated by the backend.
    blocking = _source(BLOCKING_JS)
    for chinese in ("Authorization required", "Input required", "Additional details", "Select", "Submit"):
        assert chinese in blocking
    assert "Permission required" not in blocking
    assert "Local shell" not in blocking

    permissions = _source(Path(__file__).parents[2] / "src/iac_code/web/permissions.py")
    for msgid in (
        "Allow once",
        "Deny once",
        "Always allow this session",
        "Always deny this session",
        "Always deny this tool",
        "Allow {}?",
    ):
        assert msgid in permissions
    assert "仅本次允许" not in permissions
    assert "仅本次拒绝" not in permissions


def test_elicitation_request_renders_schema_driven_form() -> None:
    # A2: MCP elicitation 请求在前端渲染成可交互面板——按 schema 字段建表单、
    # 提供 accept/decline/cancel 三个动作,并纳入 renderBlockingPanels。
    blocking = _source(BLOCKING_JS)
    assert "export function renderElicitationRequest" in blocking
    # 三个动作按钮文案(经 t() 走 i18n)。
    for label in ("Accept", "Decline", "Cancel"):
        assert label in blocking
    # 按字段类型渲染:枚举→select、布尔→checkbox、其余→input。
    assert 'field.type === "enum"' in blocking
    assert 'field.type === "boolean"' in blocking
    # 面板汇总入口必须包含 elicitations。
    panels_body = blocking.split("export function renderBlockingPanels", 1)[1]
    assert "state.elicitations" in panels_body
    assert "renderElicitationRequest(request, handlers)" in panels_body

    # app.js:把 elicitation 答案回灌到后端 /api/elicitations/{id}/answer。
    app_source = _source(APP_JS)
    assert "onElicitationAnswer" in app_source
    assert "api.answerElicitation(requestId, answer)" in app_source
    # 状态水合:从会话快照恢复 pendingElicitations,并纳入 pending 计数。
    assert "session.pendingElicitations" in app_source
    assert "pendingElicitationCount" in app_source

    # api.js:answerElicitation POST 到 elicitation 应答端点。
    api_source = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/api.js")
    assert "export function answerElicitation" in api_source
    assert "/api/elicitations/${encodeURIComponent(requestId)}/answer" in api_source

    # events.js:reducer 收 elicitation.request/resolved 事件。
    events_source = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/events.js")
    assert 'case "elicitation.request":' in events_source
    assert 'case "elicitation.resolved":' in events_source


def test_in_progress_tool_labels_use_present_tense() -> None:
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")

    # While a tool is still running, toolCommandText selects the present-tense phrase
    # (toolPhrase(tool, "progress")) so a running command reads "Running …" instead of the
    # past-tense "Ran …".
    command_body = tool_cards.split("export function toolCommandText", 1)[1].split("export function", 1)[0]
    assert "isToolInProgress(tool)" in command_body
    assert 'toolPhrase(tool, "progress")' in command_body

    # The grouped summary also flips to present tense when any tool is still in progress.
    group_body = tool_cards.split("export function toolGroupSummary", 1)[1].split("function toolGroupSummaryParts", 1)[
        0
    ]
    assert "tools.some(isToolInProgress)" in group_body
    assert 'toolGroupSummaryParts(tools, "progress")' in group_body


def test_live_tool_started_events_carry_message_and_turn_ids() -> None:
    events_py = _source(Path(__file__).parents[2] / "src/iac_code/web/events.py")

    # tool.started must forward messageId/turnId so the frontend attaches the tool card
    # inline to its assistant message (matching session-restore), instead of dropping it
    # into the detached bottom activity stack.
    started_body = events_py.split("def tool_started", 1)[1].split("def ", 1)[0]
    assert "message_id: str | None = None" in started_body
    assert "turn_id: str | None = None" in started_body
    assert 'payload["messageId"] = message_id' in started_body
    assert 'payload["turnId"] = turn_id' in started_body

    # The ToolUseStartEvent translation passes the current message id and turn id.
    _after_start = events_py.split("if isinstance(event, ToolUseStartEvent):", 1)[1]
    translate_body = _after_start.split("if isinstance(event,", 1)[0]
    assert "message_id=self._current_message_id" in translate_body
    assert "turn_id=turn_id" in translate_body


def test_active_thinking_shows_shimmering_label() -> None:
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # While a message is still streaming (thinking, no text/tool yet) the label reads
    # "正在思考" and carries the is-thinking flag; once done it reads "思考完成".
    # 进行中用独立 msgid「Thinking…」,与意图开关按钮的「Thinking」解耦(否则被开关译文锁成「思考」)。
    assert "function isThinkingActive" in app_source
    assert 'summaryLabel.className = "message-thinking-label"' in app_source
    assert 'active ? t("Thinking…") : t("Thinking done")' in app_source
    assert 'thinking.classList.add("is-thinking")' in app_source

    # The shimmer animation is defined and applied to the active thinking label.
    assert "@keyframes iac-shimmer-sweep" in styles
    assert ".message-thinking.is-thinking .message-thinking-label" in styles
    assert "background-clip: text" in styles


def test_completed_turn_collapses_process_into_summary() -> None:
    # A finished normal-mode turn collapses its intermediate process (thinking + tool cards)
    # into a "已处理 <时间>" summary with a divider; the final answer stays visible below.
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # Rendering helpers + turn grouping exist.
    assert "function renderCollapsedTurn" in app_source
    assert "function formatTurnDuration" in app_source
    assert "function buildThinkingElement" in app_source
    assert "function buildToolCardsElement" in app_source
    assert "function buildMessageBodyElement" in app_source
    assert "const flushPendingTurn" in app_source

    # The collapsed header text and duration lookup from the per-turn timing map.
    assert 't("Processed")' in app_source
    assert "state.turns?.[turnId]?.elapsedMs" in app_source
    assert '"turn-process"' in app_source

    # In-progress turn stays expanded; completed turns collapse.
    assert "flushPendingTurn(Boolean(state.currentTurnActive))" in app_source

    # 只有最后一次工具调用之后的文本才是「最终回答」;此前每个步骤的文本旁白
    # (夹在工具调用之间的 text delta)连同思考、工具一起折进「已处理」,不平铺成答案。
    assert "let lastToolIndex = -1;" in app_source
    assert "const isFinalAnswer = i > lastToolIndex;" in app_source
    assert "if (body && !isFinalAnswer) {" in app_source
    assert "if (body && isFinalAnswer) {" in app_source

    # Collapsed-turn styles: divider under the summary + rotating chevron.
    summary_block = _css_block(styles, ".turn-process-summary")
    assert "border-bottom: 1px solid var(--codex-border);" in summary_block
    assert ".turn-process[open] > .turn-process-summary .turn-process-chevron" in styles


def test_in_progress_tool_cards_get_shimmer_class() -> None:
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")
    styles = _source(STYLES_CSS)

    # In-progress tools (running/pending/executing, no results yet) are marked is-active
    # so their title shimmers like the "正在思考" label.
    assert "export function isToolInProgress" in tool_cards
    assert 'isToolInProgress(tool) ? "is-active" : ""' in tool_cards
    assert "tools.some(isToolInProgress)" in tool_cards
    assert ".tool-card.is-active > .tool-card-row .tool-card-title" in styles


def test_shimmer_animation_is_not_too_fast() -> None:
    styles = _source(STYLES_CSS)

    # The shimmer sweep was slowed down from 1.6s to reduce the "too fast" flicker.
    assert "animation: iac-shimmer-sweep 2.8s linear infinite;" in styles
    assert "animation: iac-shimmer-sweep 1.6s linear infinite;" not in styles


def test_shimmer_phase_survives_per_frame_rebuild() -> None:
    # Root cause of「看不到滑光」: 活动轮次里 message-stack 每帧全量重建,流光标题元素随之重建,
    # CSS 动画从头(background-position:180%,亮带在文本右侧不可见)重启,高频重渲染下亮带永远
    # 进不了可视区。修复:用 performance.now() 推导负 animation-delay,让新建元素续到当前相位。
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # 助手函数:导出、以 performance.now() 推导负延迟、周期与 CSS keyframes 时长一致。
    assert "export function applyShimmerPhase(el)" in tool_cards
    assert "export const SHIMMER_PERIOD_S = 2.8;" in tool_cards
    assert "el.style.animationDelay = `-${((now / 1000) % SHIMMER_PERIOD_S).toFixed(3)}s`;" in tool_cards
    # 周期常量必须与 styles.css 的动画时长同步,否则相位对齐会漂移。
    assert "iac-shimmer-sweep 2.8s" in styles

    # 进行中的工具卡/工具组标题:构建时对齐相位。
    assert "applyShimmerPhase(cardTitle);" in tool_cards
    assert "applyShimmerPhase(groupTitle);" in tool_cards

    # app.js 复用同一助手,覆盖「正在思考」(active)、流水线思考占位、压缩进行中文案。
    assert 'applyShimmerPhase, applySpinPhase } from "./components/tool_cards.js' in app_source
    assert "applyShimmerPhase(summaryLabel);" in app_source
    assert "applyShimmerPhase(label);" in app_source


def test_spin_phase_survives_per_frame_rebuild() -> None:
    # 同源问题(转圈版): 侧栏会话列表 / 命令面板 / 流水线步骤 / 会话加载的转圈都挂在会被
    # replaceChildren 全量重建的容器里。每次 render / 后台刷新都重造 spinner <span>,CSS 旋转动画
    # 从 0° 重启——用户看到「转着转着被拽回原点又重转」。修复:用 performance.now() 推导负
    # animation-delay,让新建元素续到当前相位(与 applyShimmerPhase 同法,但周期按各 spinner 传入)。
    tool_cards = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/tool_cards.js")
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    # 助手函数:导出、按传入周期以 performance.now() 推导负延迟。
    assert "export function applySpinPhase(el, periodS)" in tool_cards
    assert "el.style.animationDelay = `-${((now / 1000) % periodS).toFixed(3)}s`;" in tool_cards

    # 传入周期必须与 styles.css 的转圈动画时长一致,否则相位对齐会漂移。
    assert "iac-thread-spin 1.4s" in styles  # .thread-spinner(侧栏/命令面板/流水线步骤)
    assert "iac-thread-spin 0.85s" in styles  # .message-loading-spinner(会话加载)

    # app.js 在每处新建 spinner 后对齐相位:1.4s 的 thread-spinner 与 0.85s 的加载转圈。
    assert 'applyShimmerPhase, applySpinPhase } from "./components/tool_cards.js' in app_source
    assert "applySpinPhase(spinner, 1.4)" in app_source
    assert "applySpinPhase(spinner, 0.85)" in app_source


def test_permission_panel_is_readable_on_dark_theme() -> None:
    styles = _source(STYLES_CSS)

    # The permission title and detail text must be readable on the dark background:
    # a dedicated dark-theme block sets the h3 to the light codex text color and keeps the
    # permission panel on the neutral codex surface, fixing the invisible dark-on-dark text.
    dark_block = styles.split("授权 / 提问面板", 1)
    assert len(dark_block) == 2, "expected the dark-theme permission panel block"
    block = dark_block[1]
    assert ".blocking-panel h3" in block
    assert "color: var(--codex-text);" in block
    assert ".blocking-panel-permission" in block
    # Task 10 把该白覆盖层转为字节安全的 color-mix(深色主题下与 rgba(255,255,255,.045) 逐字节一致)。
    assert "background: color-mix(in srgb, var(--codex-ink) 4.5%, transparent);" in block
    assert ".blocking-detail" in block


def test_composer_has_single_send_stop_button() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    composer_source = _source(Path(__file__).parents[2] / "src/iac_code/web/static/js/components/composer.js")
    styles = _source(STYLES_CSS)

    # There is exactly one primary composer button; the separate Stop / Interrupt
    # buttons are gone.
    assert 'data-app-shell="composer-send"' in html
    assert 'data-app-shell="composer-stop"' not in html
    assert 'data-app-shell="composer-interrupt"' not in html
    assert 'byShell("composer-stop")' not in app_source
    assert 'byShell("composer-interrupt")' not in app_source

    # The send button carries both a send glyph and a stop (square) glyph that swap.
    assert "composer-icon-send" in html
    assert "composer-icon-stop" in html
    assert ".send-action.is-stopping .composer-icon-stop" in styles

    # While a turn is active the send button becomes the stop control.
    assert 'sendButton.classList.toggle("is-stopping", turnActive)' in composer_source
    assert "void stopCurrentTurn();" in composer_source


def test_web_opens_to_new_session_draft_by_default() -> None:
    app_source = _source(APP_JS)

    # Opening the web page should always land on the new-session draft, never auto-select
    # the most recent existing session.
    boot_tail = app_source.split("const sessions = await loadSessions();")
    assert len(boot_tail) == 1, "boot must not fetch sessions[0] to auto-switch on load"
    assert "await switchSession(displaySessionId(sessions[0]))" not in app_source
    boot_region = app_source.split("await loadSessions();")[-1]
    assert "startNewSessionDraft();" in boot_region


def test_pipeline_replay_no_longer_hides_complete_step() -> None:
    session_manager = _source(Path(__file__).parents[2] / "src/iac_code/web/session_manager.py")

    # complete_step must NOT be filtered out during pipeline transcript replay anymore,
    # otherwise its conclusion card never reaches the restored main transcript.
    assert "PIPELINE_HIDDEN_REPLAY_TOOL_NAMES: set[str] = set()" in session_manager
    assert 'PIPELINE_HIDDEN_REPLAY_TOOL_NAMES = {"complete_step"}' not in session_manager


def test_sidebar_is_resizable_with_draggable_handle() -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)
    app_source = _source(APP_JS)

    # Handle must exist in the shell and be discoverable by the app wiring.
    assert 'data-app-shell="sidebar-resize"' in html
    assert 'class="sidebar-resize-handle"' in html

    # Width is driven by a CSS variable so JS can update it live.
    assert "--rail-width: 264px" in styles
    assert "clamp(var(--rail-min-width), var(--rail-width), var(--rail-max-width)) minmax(0, 1fr)" in styles
    assert ".sidebar-resize-handle {" in styles
    assert "cursor: col-resize" in styles
    assert "body.is-resizing-sidebar" in styles

    # JS drives the variable, clamps, and persists the chosen width.
    assert "setupSidebarResize" in app_source
    assert 'setProperty("--rail-width"' in app_source
    assert "iac-code:rail-width" in app_source
    assert "pointerdown" in app_source
    assert "is-resizing-sidebar" in app_source


def test_project_row_icon_matches_new_session_project_picker() -> None:
    app_source = _source(APP_JS)

    # Collapsed projects must reuse the exact new-session project-picker glyph
    # classes rather than a bespoke copy, so the two icons stay identical.
    assert 'collapsed ? "draft-session-menu-icon is-project" : "project-row-icon"' in app_source


def test_expanded_project_uses_open_notebook_icon() -> None:
    styles = _source(STYLES_CSS)
    svg = Path(__file__).parents[2] / "src/iac_code/web/static/icons/sidebar-project-open.svg"

    assert svg.exists(), "expanded project icon svg must exist"
    assert '--codex-project-open-icon: url("/static/icons/sidebar-project-open.svg")' in styles

    icon_block = _css_block(styles, ".project-row-icon")
    assert "-webkit-mask: var(--codex-project-open-icon)" in icon_block
    assert "mask: var(--codex-project-open-icon)" in icon_block


def test_project_context_menu_actions_and_apis() -> None:
    app_source = _source(APP_JS)
    api_source = _source(API_JS)
    styles = _source(STYLES_CSS)

    # API client exposes the project-level endpoints.
    assert "export function updateProject" in api_source
    assert '"/api/projects"' in api_source
    assert "export function revealProject" in api_source
    assert '"/api/projects/reveal"' in api_source
    # Archiving a project archives all of its sessions (session-level), not the
    # project itself, so they surface grouped in the 「已归档对话」panel.
    assert "export function archiveProjectSessions" in api_source
    assert '"/api/projects/archive-sessions"' in api_source

    # The "…" popover wires all five actions (create-worktree intentionally omitted).
    assert "openProjectMenu" in app_source
    for label in [
        't("Pin project")',
        't("Unpin project")',
        't("Reveal in Finder")',
        't("Rename project")',
        't("Archive conversation")',
        't("Remove")',
    ]:
        assert label in app_source
    assert "创建永久工作树" not in app_source
    assert "toggleProjectPinned" in app_source
    assert "archiveProjectGroup" in app_source
    # 「归档对话」走会话级归档端点,而非把项目本身标记归档。
    assert "api.archiveProjectSessions(group.key)" in app_source
    assert "removeProjectGroup" in app_source
    assert "revealProjectGroup" in app_source
    assert "startProjectRename" in app_source
    assert "openAppModal" in app_source

    # Collapse state is persisted through the project metadata endpoint.
    assert "toggleProjectCollapsed" in app_source
    assert "api.updateProject(key, { collapsed })" in app_source

    # Pinned area mixes pinned sessions and pinned projects, ordered by pin time.
    assert "normalizePinnedProjects" in app_source
    assert "pinnedProjects" in app_source

    # Popover styling exists.
    assert ".project-menu-popover" in styles
    assert ".pmi-pin" in styles
    assert ".pmi-remove" in styles


def test_edit_queued_input_conflict_refreshes_with_chinese_notice() -> None:
    # 编辑排队消息时,若 agent 已消费了一条(某回合结束)导致队列下标/原文本失效,
    # 后端返回 409。前端必须像删除/引导一样刷新队列,并给出中文说明,而不是把后端的
    # 英文 "queued input changed; please retry" 直接抛给用户、把弹窗停在无法成功重试的状态。
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    assert "editQueuedInput" in app_source
    assert "error?.status === 409" in app_source
    # 冲突提示走应用内轻量 toast,而非浏览器原生 alert(带域名前缀、样式无法控制)。
    assert "showAppToast" in app_source
    assert "The queued messages changed (one has started processing or was modified). Please edit again." in app_source
    assert 'window.alert?.("排队消息' not in app_source
    # editQueuedRow 内部自行处理 409(刷新 + 中文提示),不再让后端英文原文外泄。
    assert "queued input changed; please retry" not in app_source
    # toast 样式存在,可淡入淡出。
    assert ".app-toast {" in styles
    assert ".app-toast.is-visible {" in styles


def test_project_thread_scrollbar_hugs_the_rail_edge() -> None:
    styles = _source(STYLES_CSS)

    # The scroll container is pulled out to the rail edge (negative margin) and
    # the content is re-inset with matching padding so the overlay scrollbar
    # hugs the border instead of floating ~7px inside it.
    assert "margin-right: -0.34rem;\n  padding-right: 0.34rem;" in styles


def test_composer_switch_list_follows_usable_green_dot() -> None:
    # 会话切换列表须与设置里的绿点(usable)对齐:凡 usable 且有可选模型的 provider 都能切换
    # (如仅填了 key、未保存配置的 provider),而不再只看 configured。当前 active 始终保留。
    source = _source(COMPOSER_JS)

    assert "switchableProviderItems" in source
    assert "hasSelectableModel" in source
    assert "provider?.usable && hasSelectableModel(provider)" in source
    assert "provider?.key === activeProviderKey" in source
    # 旧的「仅 configured」判定不得再作为切换列表的唯一依据。
    assert "configuredProviderItems" not in source


def test_composer_partner_source_is_switchable_and_locks_in_without_model() -> None:
    # 合作方源(第三方登录托管)也应出现在会话切换列表:点击即全局锁定(setActiveProvider),
    # 并清掉本会话的会话级覆盖(clearSessionModel),无需选 model。
    source = _source(COMPOSER_JS)

    assert "isPartnerProvider" in source
    assert "isPartnerProvider(provider)" in source
    # 合作方源纳入切换列表。
    assert "isPartnerProvider(provider) ||" in source
    # 选中合作方源:全局激活 + 清会话覆盖,不落到需要 model 的分支。
    assert "activatePartnerSelection" in source
    assert "await api.setActiveProvider(provider.key)" in source
    assert "await api.clearSessionModel(sessionId)" in source
    # 当前生效的合作方源(active.provider 为空)靠 current 标记识别并显示。
    assert "currentPartnerProvider" in source


def test_composer_refreshes_providers_after_settings_modal_closes() -> None:
    # Issue 1:设置里配置好 provider 变绿后,回到会话应立即可切换,无需刷新整页。
    composer_source = _source(COMPOSER_JS)
    app_source = _source(APP_JS)

    assert "refreshProviders()" in composer_source
    assert "composer?.refreshProviders?.()" in app_source


def test_api_exposes_clear_session_model_endpoint() -> None:
    source = _source(API_JS)

    assert "export function clearSessionModel" in source
    assert '"/model"' in source
    assert '"DELETE"' in source


def test_composer_handles_send_queue_interrupt_attachments_and_suggestions() -> None:
    source = _source(COMPOSER_JS)

    assert "Shift+Enter" in source
    assert 'event.key === "Enter"' in source
    assert "postMessage" in source
    assert "postQueuedInput" in source
    assert "postInterrupt" in source
    assert "postCommand" in source
    assert "getSuggestions" in source
    assert "uploadImage" in source
    assert "makeFileReferenceAttachment" in source
    assert "queued_attachment_not_supported" in source
    assert source.count("queued_attachment_not_supported") == 1
    # 提示走 t() 本地化(英文 msgid),且回合结束/移除附件后会被清除,不再残留。
    assert 't("Attachments can be sent after the current turn finishes.")' in source
    assert "附件需等当前回合结束后再发送。" not in source
    assert "clearQueuedAttachmentError" in source
    assert "isMidTurnCommandLike" in source
    assert "imageIds" in source
    assert "fileRefs" in source
    assert "unsupported_image" in source
    assert "visibleSuggestions.slice(0, 5)" not in source
    assert "scrollIntoView" in source
    assert "suggestion-icon" in source
    assert "suggestionIconClasses" in source
    assert "commandNameFromSuggestion" in source
    assert "suggestionDisplayParts" in source
    assert "suggestionMenuSections" in source
    assert "visibleComposerSuggestions" in source
    assert "skillScopeLabel" in source
    assert "HIDDEN_COMPOSER_COMMAND_NAMES" in source
    assert "SESSION_ONLY_COMMAND_NAMES" in source
    # 流水线会话隐藏 /compact:命令集 + 过滤入参 + app.js 传入的模式判定回调。
    assert 'PIPELINE_HIDDEN_COMMAND_NAMES = new Set(["compact"])' in source
    assert "pipelineMode: Boolean(options.isPipelineMode?.())" in source
    assert "syncSuggestionActiveState" in source
    assert 'addEventListener("mouseenter"' in source
    assert 'addEventListener("pointerenter"' in source
    assert 'addEventListener("mousemove"' in source
    assert "is-command-compact" in source
    assert "is-command-effort" in source
    assert "is-skill-suggestion" in source
    assert "suggestion-section-label" in source
    assert "suggestion-scope" in source
    assert 'event.key === "ArrowDown"' in source
    assert 'event.key === "ArrowUp"' in source
    assert 'event.key === "Tab"' in source
    assert 'event.key === "Escape"' in source
    assert 'addEventListener("paste"' in source
    assert 'addEventListener("drop"' in source


def test_composer_disables_send_until_text_or_attachment_exists(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                if (force) {
                  this.items.add(name);
                } else {
                  this.items.delete(name);
                }
                this.owner.className = [...this.items].join(" ");
              }
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, ...extra });
                }
              }
              querySelector() {
                return null;
              }
              focus() {}
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const form = new Element("form");
            const textarea = new Element("textarea");
            const sendButton = new Element("button");
            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton,
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                getSuggestions() {
                  return Promise.resolve({ suggestions: [] });
                },
                uploadImage() {
                  return Promise.resolve({ imageId: "img-1" });
                },
              },
            );
            controller.setSession("S");
            const initiallyDisabled = sendButton.disabled;

            textarea.value = "  deploy vpc  ";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            const enabledWithText = !sendButton.disabled;

            textarea.value = "   ";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            const disabledAfterClearing = sendButton.disabled;

            await controller.addFiles([{ name: "diagram.png", type: "image/png" }]);
            const enabledWithAttachment = !sendButton.disabled;

            console.log(JSON.stringify({
              initiallyDisabled,
              enabledWithText,
              disabledAfterClearing,
              enabledWithAttachment,
              formClassName: form.className,
            }));
            """
        ),
    )

    assert output == {
        "initiallyDisabled": True,
        "enabledWithText": True,
        "disabledAfterClearing": True,
        "enabledWithAttachment": True,
        "formClassName": "has-attachments",
    }


def test_composer_clears_session_scoped_draft_state_when_switching_sessions(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                if (force) {
                  this.items.add(name);
                } else {
                  this.items.delete(name);
                }
                this.owner.className = [...this.items].join(" ");
              }
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, ...extra });
                }
              }
              querySelector() {
                return null;
              }
              querySelectorAll() {
                return [];
              }
              focus() {}
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const form = new Element("form");
            const textarea = new Element("textarea");
            const chips = new Element("div");
            const skillRow = new Element("div");
            const suggestions = new Element("div");
            const sendButton = new Element("button");
            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton,
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: chips,
                skillRow,
                suggestions,
                errorTarget: new Element("output"),
              },
              {
                getSuggestions() {
                  return Promise.resolve({
                    suggestions: [
                      { kind: "skill", value: "$Brainstorming", label: "Brainstorming", origin: "bundled" },
                    ],
                  });
                },
                uploadImage() {
                  return Promise.resolve({ imageId: "image-owned-by-A" });
                },
              },
            );

            controller.setSession("A");
            await controller.addFiles([{ name: "a.png", type: "image/png" }]);
            textarea.value = "$Brain";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            await Promise.resolve();
            textarea.dispatch("keydown", { key: "Enter", shiftKey: false });
            textarea.value = "/";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            await Promise.resolve();

            controller.setSession("B");

            console.log(JSON.stringify({
              textareaValue: textarea.value,
              chipCount: chips.children.length,
              skillCount: skillRow.children.length,
              skillHidden: skillRow.hidden,
              suggestionsCount: suggestions.children.length,
              suggestionsHidden: suggestions.hidden,
              formClassName: form.className,
              sendDisabled: sendButton.disabled,
            }));
            """
        ),
    )

    assert output == {
        "textareaValue": "",
        "chipCount": 0,
        "skillCount": 0,
        "skillHidden": True,
        "suggestionsCount": 0,
        "suggestionsHidden": True,
        "formClassName": "",
        "sendDisabled": True,
    }


def test_composer_stale_submit_does_not_clear_new_session_attachments_or_error(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                force ? this.items.add(name) : this.items.delete(name);
                this.owner.className = [...this.items].join(" ");
              }
            }
            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) { this.children.push(...children); }
              replaceChildren(...children) { this.children = children; }
              setAttribute(name, value) { this[name] = String(value); }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() { return null; }
              querySelectorAll() { return []; }
              focus() {}
            }

            globalThis.document = { createElement(tagName) { return new Element(tagName); } };
            globalThis.window = {
              getComputedStyle() { return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" }; },
            };

            let resolvePost;
            const postFinished = new Promise((resolve) => { resolvePost = resolve; });
            let markPostStarted;
            const postStarted = new Promise((resolve) => { markPostStarted = resolve; });
            const form = new Element("form");
            const textarea = new Element("textarea");
            const chips = new Element("div");
            const errorTarget = new Element("output");
            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: chips,
                suggestions: new Element("div"),
                errorTarget,
              },
              {
                postMessage() {
                  markPostStarted();
                  return postFinished;
                },
                uploadImage(_sessionId, file) { return Promise.resolve({ imageId: `image-${file.name}` }); },
              },
            );

            controller.setSession("A");
            textarea.value = "message for A";
            const submitA = controller.submit();
            await postStarted;

            controller.setSession("B");
            textarea.value = "draft for B";
            await controller.addFiles([{ name: "b.png", type: "image/png" }]);
            await controller.addFiles([{ name: "bad.txt", type: "text/plain" }]);
            resolvePost({ accepted: true });
            await submitA;

            console.log(JSON.stringify({
              textareaValue: textarea.value,
              chipCount: chips.children.length,
              errorText: errorTarget.textContent,
              formClassName: form.className,
            }));
            """
        ),
    )

    assert output == {
        "textareaValue": "draft for B",
        "chipCount": 1,
        "errorText": "Use @ suggestions for workspace file references.",
        "formClassName": "has-attachments",
    }


def test_composer_restored_draft_survives_late_submit_completion(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) { this.owner = owner; this.items = new Set(); }
              toggle(name, force) {
                force ? this.items.add(name) : this.items.delete(name);
                this.owner.className = [...this.items].join(" ");
              }
            }
            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) { this.children.push(...children); }
              replaceChildren(...children) { this.children = children; }
              setAttribute(name, value) { this[name] = String(value); }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() { return null; }
              querySelectorAll() { return []; }
              focus() {}
            }

            globalThis.document = { createElement(tagName) { return new Element(tagName); } };
            globalThis.window = {
              getComputedStyle() { return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" }; },
            };

            let resolvePost;
            const postFinished = new Promise((resolve) => { resolvePost = resolve; });
            let markPostStarted;
            const postStarted = new Promise((resolve) => { markPostStarted = resolve; });
            const form = new Element("form");
            const textarea = new Element("textarea");
            const chips = new Element("div");
            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: chips,
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                postMessage() {
                  markPostStarted();
                  return postFinished;
                },
              },
            );

            controller.setSession("A");
            textarea.value = "message for A";
            const submitA = controller.submit();
            await postStarted;

            controller.restoreDraft({
              draft: "must survive",
              imageIds: ["image-restored"],
              fileRefs: ["main.tf"],
            });
            resolvePost({ accepted: true });
            await submitA;

            console.log(JSON.stringify({
              textareaValue: textarea.value,
              chipCount: chips.children.length,
              chipTitles: chips.children.map((chip) => chip.title),
              formClassName: form.className,
            }));
            """
        ),
    )

    assert output == {
        "textareaValue": "must survive",
        "chipCount": 2,
        "chipTitles": ["image-restored", "@ main.tf"],
        "formClassName": "has-attachments",
    }


def test_switching_to_already_active_session_preserves_composer_draft() -> None:
    app_source = _source(APP_JS)
    switch_region = app_source.split("async function switchSession(sessionId) {")[-1].split(
        "\nfunction startNewSessionDraft", 1
    )[0]

    same_session_guard = "if (sessionId === state.currentSessionId)"
    assert same_session_guard in switch_region
    assert switch_region.index(same_session_guard) < switch_region.index("++sessionLoadGeneration")
    assert switch_region.index(same_session_guard) < switch_region.index('composer?.setDraft("", { force: true })')


def test_user_message_attachments_render_in_transcript() -> None:
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    assert "function buildMessageAttachmentsElement" in app_source
    assert "message.imageIds" in app_source
    assert "message.fileRefs" in app_source
    assert 'className = "message-attachments"' in app_source
    assert "/api/images/" in app_source
    assert ".message-attachments" in styles


def test_stored_message_normalizer_carries_attachment_ids() -> None:
    # 会话恢复路径:load_visible_transcript 会把用户图片/文件附件以 imageIds/fileRefs
    # (camelCase,与实时 user.message 事件同名)挂在转录行上,normalizeStoredMessage 必须把它们
    # 透传到规整后的消息对象,否则 buildMessageAttachmentsElement 读到 undefined→不渲染图片,
    # 表现为「会话恢复图片不显示」。实时路径(events.js 的 user.message)一直设置这两个字段,
    # 故 live 正常、reload 丢图,是 reload-vs-live 不一致。
    app_source = _source(APP_JS)
    normalizer = app_source.split("function normalizeStoredMessage(", 1)[1].split("\nfunction ", 1)[0]

    assert "message.imageIds" in normalizer
    assert "message.fileRefs" in normalizer


def test_composer_uses_contextual_placeholder_without_visible_label(tmp_path: Path) -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)

    assert "Ask IaC Code" not in html
    assert 'aria-label="Enter your requirement"' in html
    assert 'placeholder="Describe your infrastructure needs"' in html
    assert ".composer-topline:has(.composer-error:not(:empty))" in styles

    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              toggle() {}
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.placeholder = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList();
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() {
                return null;
              }
              focus() {}
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const textarea = new Element("textarea");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea,
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                getSuggestions() {
                  return Promise.resolve({ suggestions: [] });
                },
              },
              {
                createSessionForSubmit() {
                  return Promise.resolve({ webSessionId: "draft-web-session" });
                },
              },
            );

            const initialPlaceholder = textarea.placeholder;
            controller.setSession("active-session");
            const existingSessionPlaceholder = textarea.placeholder;
            controller.setSession("");
            const draftPlaceholder = textarea.placeholder;

            console.log(JSON.stringify({
              initialPlaceholder,
              existingSessionPlaceholder,
              draftPlaceholder,
            }));
            """
        ),
    )

    assert output == {
        "initialPlaceholder": "Describe your infrastructure needs",
        "existingSessionPlaceholder": "Continue adding or adjusting requirements",
        "draftPlaceholder": "Describe your infrastructure needs",
    }


def test_composer_filters_slash_commands_for_new_and_existing_sessions(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { commandNameFromSuggestion, visibleComposerSuggestions } = await import(__COMPOSER_MODULE__);

            const names = [
              "auth",
              "clear",
              "compact",
              "debug",
              "effort",
              "exit",
              "help",
              "iac-aliyun",
              "memory",
              "model",
              "rename",
              "resume",
              "skills",
              "status",
            ];
            const rawSuggestions = names.map((name) => ({
              kind: "command",
              value: `/${name} `,
              label: `${name} command`,
            }));
            const draft = visibleComposerSuggestions(rawSuggestions, { draftSessionActive: true })
              .map(commandNameFromSuggestion);
            const existing = visibleComposerSuggestions(rawSuggestions, { draftSessionActive: false })
              .map(commandNameFromSuggestion);
            // 流水线会话:/compact 无法主动执行,从菜单移除;其余会话级命令(status)仍在。
            const pipeline = visibleComposerSuggestions(rawSuggestions, { pipelineMode: true })
              .map(commandNameFromSuggestion);

            console.log(JSON.stringify({ draft, existing, pipeline }));
            """
        ),
    )

    assert output == {
        "draft": ["iac-aliyun"],
        "existing": ["compact", "iac-aliyun", "status"],
        "pipeline": ["iac-aliyun", "status"],
    }


def test_blocked_composer_command_name_mirrors_menu_filter(tmp_path: Path) -> None:
    # 直接输入提交(绕过「/」补全菜单)也要与菜单同门:菜单按运行时状态隐藏两类命令——
    # 流水线模式下的 /compact,以及新会话草稿阶段的 SESSION_ONLY 命令(clear/compact/status/mcp)。
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { blockedComposerCommandName } = await import(__COMPOSER_MODULE__);
            console.log(JSON.stringify({
              pipelineCompact: blockedComposerCommandName("/compact", { pipelineMode: true }),
              pipelineCompactSpaced: blockedComposerCommandName("  /compact now", { pipelineMode: true }),
              pipelineStatus: blockedComposerCommandName("/status", { pipelineMode: true }),
              draftCompact: blockedComposerCommandName("/compact", { draftSessionActive: true }),
              draftStatus: blockedComposerCommandName("/status", { draftSessionActive: true }),
              draftClear: blockedComposerCommandName("/clear", { draftSessionActive: true }),
              draftMcp: blockedComposerCommandName("/mcp", { draftSessionActive: true }),
              draftUnknown: blockedComposerCommandName("/whatever", { draftSessionActive: true }),
              activeCompact: blockedComposerCommandName("/compact", {}),
              activeStatus: blockedComposerCommandName("/status", {}),
              plainMessage: blockedComposerCommandName("hello world", { pipelineMode: true, draftSessionActive: true }),
            }));
            """
        ),
    )

    assert output == {
        "pipelineCompact": {"command": "compact", "reason": "pipeline"},
        "pipelineCompactSpaced": {"command": "compact", "reason": "pipeline"},
        "pipelineStatus": None,
        "draftCompact": {"command": "compact", "reason": "draft"},
        "draftStatus": {"command": "status", "reason": "draft"},
        "draftClear": {"command": "clear", "reason": "draft"},
        "draftMcp": {"command": "mcp", "reason": "draft"},
        "draftUnknown": None,
        "activeCompact": None,
        "activeStatus": None,
        "plainMessage": None,
    }


def test_composer_blocks_typed_session_only_command_at_submit(tmp_path: Path) -> None:
    # 回归:流水线阶段 / 新会话草稿阶段直接键入 /compact 并提交,不能绕过菜单过滤去执行命令;
    # 应拦截、给出对应提示,且完全不调用 postCommand、不建会话。已有普通会话则照常执行。
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) { this.owner = owner; this.items = new Set(); }
              toggle(name, force) {
                force ? this.items.add(name) : this.items.delete(name);
                this.owner.className = [...this.items].join(" ");
              }
            }
            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) { this.children.push(...children); }
              replaceChildren(...children) { this.children = children; }
              setAttribute(name, value) { this[name] = String(value); }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() { return null; }
              querySelectorAll() { return []; }
              focus() {}
            }

            globalThis.document = { createElement(tagName) { return new Element(tagName); } };
            globalThis.window = {
              getComputedStyle() { return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" }; },
            };

            function makeController(composerOptions) {
              const errorTarget = new Element("output");
              const textarea = new Element("textarea");
              const calls = [];
              const controller = createComposerController(
                {
                  form: new Element("form"),
                  textarea,
                  sendButton: new Element("button"),
                  fileInput: new Element("input"),
                  attachmentChips: new Element("div"),
                  suggestions: new Element("div"),
                  errorTarget,
                },
                {
                  postCommand(sessionId, command) {
                    calls.push({ sessionId, command });
                    return Promise.resolve({ ok: true });
                  },
                },
                composerOptions,
              );
              return { controller, textarea, errorTarget, calls };
            }

            const pipeline = makeController({ isPipelineMode: () => true });
            pipeline.controller.setSession("pipeline-session");
            pipeline.textarea.value = "/compact";
            await pipeline.controller.submit();

            // 新会话草稿阶段(普通模式,尚未落地会话):截图里的场景。
            const draft = makeController({ isDraftSessionActive: () => true });
            draft.controller.setSession("draft-session");
            draft.textarea.value = "/compact";
            await draft.controller.submit();

            const normal = makeController({});
            normal.controller.setSession("normal-session");
            normal.textarea.value = "/compact";
            await normal.controller.submit();

            console.log(JSON.stringify({
              pipelineCalls: pipeline.calls,
              pipelineError: pipeline.errorTarget.textContent,
              draftCalls: draft.calls,
              draftError: draft.errorTarget.textContent,
              draftTextarea: draft.textarea.value,
              normalCalls: normal.calls,
              normalError: normal.errorTarget.textContent,
            }));
            """
        ),
    )

    assert output["pipelineCalls"] == []
    assert output["pipelineError"] == "/compact is not available in pipeline mode"
    assert output["draftCalls"] == []
    assert output["draftError"] == "/compact is only available in an active conversation"
    assert output["draftTextarea"] == "/compact"
    assert output["normalCalls"] == [{"sessionId": "normal-session", "command": "/compact"}]
    assert output["normalError"] == ""


def test_composer_groups_slash_skills_and_labels_skill_origin(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const {
              commandNameFromSuggestion,
              skillScopeLabel,
              orderedComposerSuggestions,
              suggestionDisplayParts,
              suggestionIconClasses,
              suggestionLayoutKind,
              suggestionMenuSections,
              visibleComposerSuggestions,
            } = await import(__COMPOSER_MODULE__);

            const rawSuggestions = [
              {
                kind: "command",
                value: "/clear",
                label: "clear 清除对话历史",
              },
              {
                kind: "command",
                value: "/compact",
                label: "compact Compact conversation context",
              },
              {
                kind: "command",
                value: "/iac-aliyun",
                label: "iac-aliyun 阿里云模板生成",
                origin: "bundled",
              },
              {
                kind: "command",
                value: "/status",
                label: "status Show current session status",
              },
              {
                kind: "command",
                value: "/user-skill",
                label: "user-skill 用户技能",
                origin: "user",
              },
              {
                kind: "skill",
                value: "$project-skill",
                label: "project-skill 项目技能",
                origin: "project",
              },
            ];

            const visible = visibleComposerSuggestions(rawSuggestions, { draftSessionActive: false });
            console.log(JSON.stringify({
              commandNames: visible.map(commandNameFromSuggestion),
              layoutKinds: visible.map(suggestionLayoutKind),
              scopeLabels: visible.map(skillScopeLabel),
              sections: suggestionMenuSections(visible).map((section) => ({
                kind: section.kind,
                label: section.label,
                commandNames: section.suggestions.map(commandNameFromSuggestion),
              })),
              orderedCommandNames: orderedComposerSuggestions(visible).map(commandNameFromSuggestion),
              commandTokens: visible
                .filter((suggestion) => suggestionLayoutKind(suggestion) === "command")
                .map((suggestion) => suggestionDisplayParts(suggestion).token),
              commandDescriptions: visible
                .filter((suggestion) => suggestionLayoutKind(suggestion) === "command")
                .map((suggestion) => suggestionDisplayParts(suggestion).description),
              statusIconClasses: suggestionIconClasses(
                visible.find((suggestion) => commandNameFromSuggestion(suggestion) === "status"),
              ),
              skillIconClasses: suggestionIconClasses(
                visible.find((suggestion) => commandNameFromSuggestion(suggestion) === "iac-aliyun"),
              ),
            }));
            """
        ),
    )

    assert output == {
        "commandNames": ["compact", "iac-aliyun", "status", "user-skill", "project-skill"],
        "layoutKinds": ["command", "skill", "command", "skill", "skill"],
        "scopeLabels": ["", "System", "", "Personal", "Project"],
        "sections": [
            {"kind": "command", "label": "", "commandNames": ["compact", "status"]},
            {"kind": "skill", "label": "Skills", "commandNames": ["iac-aliyun", "user-skill", "project-skill"]},
        ],
        "orderedCommandNames": ["compact", "status", "iac-aliyun", "user-skill", "project-skill"],
        "commandTokens": ["Compact", "Status"],
        "commandDescriptions": ["Compact this session's context", "Show current session status"],
        "statusIconClasses": "suggestion-icon is-command is-command-status",
        "skillIconClasses": "suggestion-icon is-skill",
    }


def test_composer_displays_context_usage_for_compact_and_model_control(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const {
              contextUsagePercent,
              createComposerController,
              suggestionDisplayParts,
            } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                if (force) {
                  this.items.add(name);
                } else {
                  this.items.delete(name);
                }
                this.owner.className = [...this.items].join(" ");
              }
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = {
                  values: {},
                  setProperty(name, value) {
                    this.values[name] = value;
                  },
                };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() {
                return null;
              }
              focus() {}
            }

            function textOf(node) {
              const childText = (node.children || []).map(textOf).join(" ");
              return `${node.textContent || ""} ${childText}`.replace(/\\s+/g, " ").trim();
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const contextUsage = { totalTokens: 34000, contextWindow: 131072 };
            const modelControl = new Element("button");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea: new Element("textarea"),
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
                modelControl,
                modelMenu: new Element("div"),
              },
              {
                getProviders() {
                  return Promise.resolve({
                    active: {
                      provider: "openai",
                      model: "gpt-5.5",
                      effort: "high",
                      hasApiKey: true,
                    },
                    providers: [
                      {
                        key: "openai",
                        name: "OpenAI",
                        configured: true,
                        models: [{ id: "gpt-5.5", name: "GPT-5.5", efforts: ["high"] }],
                      },
                    ],
                  });
                },
              },
            );
            controller.setContextUsage(contextUsage);
            await new Promise((resolve) => setTimeout(resolve, 0));

            const icon = modelControl.children[0];
            const label = modelControl.children[1];
            const compactParts = suggestionDisplayParts(
              { kind: "command", value: "/compact", label: "compact 压缩对话上下文" },
              { contextUsage },
            );

            console.log(JSON.stringify({
              percent: contextUsagePercent(contextUsage),
              compactDescription: compactParts.description,
              modelText: textOf(modelControl),
              iconClass: icon?.className,
              iconDegrees: icon?.style?.values?.["--context-usage-degrees"],
              labelClass: label?.className,
            }));
            """
        ),
    )

    assert output == {
        "percent": 26,
        "compactDescription": "Compact this session's context(Used 26%)",
        "modelText": "GPT-5.5 High",
        "iconClass": "context-usage-icon composer-model-usage-icon",
        "iconDegrees": "93.6deg",
        "labelClass": "composer-model-control-label",
    }


def test_composer_renders_one_context_usage_ring_per_pipeline_step(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                if (force) {
                  this.items.add(name);
                } else {
                  this.items.delete(name);
                }
                this.owner.className = [...this.items].join(" ");
              }
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = {
                  values: {},
                  setProperty(name, value) {
                    this.values[name] = value;
                  },
                };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() {
                return null;
              }
              focus() {}
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const modelControl = new Element("button");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea: new Element("textarea"),
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
                modelControl,
                modelMenu: new Element("div"),
              },
              {
                getProviders() {
                  return Promise.resolve({
                    active: {
                      provider: "openai",
                      model: "gpt-5.5",
                      effort: "high",
                      hasApiKey: true,
                    },
                    providers: [
                      {
                        key: "openai",
                        name: "OpenAI",
                        configured: true,
                        models: [{ id: "gpt-5.5", name: "GPT-5.5", efforts: ["high"] }],
                      },
                    ],
                  });
                },
              },
            );

            controller.setContextUsages([
              {
                groupId: "step:step-1-1",
                level: "step",
                title: "Understand",
                candidateName: "",
                contextUsage: { totalTokens: 30000, contextWindow: 60000 },
              },
              {
                groupId: "candidate-step:cand-0-gen-1",
                level: "sub_step",
                title: "Generate",
                candidateName: "Plan A",
                contextUsage: { totalTokens: 12000, contextWindow: 60000 },
              },
            ]);
            await new Promise((resolve) => setTimeout(resolve, 0));
            const icons = modelControl.children.filter((c) => (c.className || "").includes("context-usage-icon"));

            controller.setContextUsages([]);
            await new Promise((resolve) => setTimeout(resolve, 0));
            const fallbackIcons = modelControl.children.filter(
              (c) => (c.className || "").includes("context-usage-icon"),
            );
            const fallbackTitle = fallbackIcons[0]?.title;

            // 流水线会话:无活跃步骤窗口时回退标签由 app.js 传入,不再是「普通会话」。
            controller.setContextFallbackLabel("Confirm & select");
            await new Promise((resolve) => setTimeout(resolve, 0));
            const pipelineFallbackIcons = modelControl.children.filter(
              (c) => (c.className || "").includes("context-usage-icon"),
            );

            console.log(JSON.stringify({
              ringCount: icons.length,
              firstTitle: icons[0]?.title,
              firstDegrees: icons[0]?.style?.values?.["--context-usage-degrees"],
              secondTitle: icons[1]?.title,
              fallbackRingCount: fallbackIcons.length,
              fallbackTitle,
              pipelineFallbackTitle: pipelineFallbackIcons[0]?.title,
            }));
            """
        ),
    )

    assert output == {
        "ringCount": 2,
        # 问题 #5：tooltip 追加百分比进度(有有效上限时)。30000/60000=50%,12000/60000=20%。
        "firstTitle": "Understand · 50%",
        "firstDegrees": "180deg",
        "secondTitle": "Plan A · Generate · 20%",
        "fallbackRingCount": 1,
        # 回退单主环无会话级用量(分母 0)→ 不拼百分比;未设标签时保持「普通会话」。
        "fallbackTitle": "Normal chat",
        # 设了流水线回退标签后,回退环改用该标签(选择门/步骤间隙不再误显示「普通会话」)。
        "pipelineFallbackTitle": "Confirm & select",
    }


def test_composer_suggestions_close_on_outside_click_for_commands_and_skills(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            const documentListeners = {};

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.className = "";
                this.textContent = "";
                this.value = "";
                this.hidden = true;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.parentNode = null;
                this.style = { setProperty() {} };
              }
              append(...children) {
                for (const child of children) {
                  child.parentNode = this;
                  this.children.push(child);
                }
              }
              replaceChildren(...children) {
                this.children = [];
                this.append(...children);
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, stopPropagation() {}, ...extra });
                }
              }
              contains(target) {
                if (target === this) {
                  return true;
                }
                return this.children.some((child) => child.contains?.(target));
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  const matchesSuggestionItem =
                    selector === ".suggestion-item" &&
                    String(node.className).split(/\\s+/).includes("suggestion-item");
                  if (matchesSuggestionItem) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector() {
                return null;
              }
              scrollIntoView() {}
              focus() {}
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
              addEventListener(type, handler) {
                documentListeners[type] = [...(documentListeners[type] || []), handler];
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const suggestions = new Element("div");
            const textarea = new Element("textarea");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea,
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions,
                errorTarget: new Element("output"),
              },
              {
                getSuggestions({ kind }) {
                  if (kind === "skill") {
                    return Promise.resolve({
                      suggestions: [
                        { kind: "skill", value: "$iac-aliyun", label: "iac-aliyun 技能" },
                      ],
                    });
                  }
                  return Promise.resolve({
                    suggestions: [
                      { kind: "command", value: "/compact", label: "compact 压缩" },
                    ],
                  });
                },
                uploadImage() {
                  return Promise.resolve({ imageId: "img-1" });
                },
              },
            );
            controller.setSession("S");

            textarea.value = "/";
            textarea.selectionStart = 1;
            textarea.dispatch("input");
            await Promise.resolve();
            await Promise.resolve();
            const slashOpen = suggestions.hidden === false;
            for (const handler of documentListeners.click || []) {
              handler({ type: "click", target: new Element("main") });
            }
            const slashClosed = suggestions.hidden === true;

            textarea.value = "$";
            textarea.selectionStart = 1;
            textarea.dispatch("input");
            await Promise.resolve();
            await Promise.resolve();
            const skillOpen = suggestions.hidden === false;
            for (const handler of documentListeners.click || []) {
              handler({ type: "click", target: new Element("main") });
            }
            const skillClosed = suggestions.hidden === true;

            console.log(JSON.stringify({ slashOpen, slashClosed, skillOpen, skillClosed }));
            """
        ),
    )

    assert output == {
        "slashOpen": True,
        "slashClosed": True,
        "skillOpen": True,
        "skillClosed": True,
    }


def test_composer_ignores_stale_out_of_order_suggestion_responses(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.className = "";
                this.textContent = "";
                this.value = "";
                this.disabled = false;
                this.hidden = true;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.parentNode = null;
                this.style = { setProperty() {} };
                this.classList = { toggle() {} };
              }
              append(...children) {
                for (const child of children) {
                  child.parentNode = this;
                  this.children.push(child);
                }
              }
              replaceChildren(...children) {
                this.children = [];
                this.append(...children);
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, stopPropagation() {} });
                }
              }
              contains(target) {
                return target === this || this.children.some((child) => child.contains?.(target));
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (
                    selector === ".suggestion-item"
                    && String(node.className).split(/\\s+/).includes("suggestion-item")
                  ) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) visit(child);
                };
                visit(this);
                return matches;
              }
              querySelector() { return null; }
              scrollIntoView() {}
              focus() {}
            }

            globalThis.document = {
              createElement(tagName) { return new Element(tagName); },
              addEventListener() {},
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const pending = [];
            const suggestions = new Element("div");
            const textarea = new Element("textarea");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea,
                sendButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                skillRow: new Element("div"),
                suggestions,
                errorTarget: new Element("output"),
              },
              {
                getSuggestions(request) {
                  return new Promise((resolve) => pending.push({ request, resolve }));
                },
              },
            );
            controller.setSession("S");

            textarea.value = "/";
            textarea.selectionStart = 1;
            textarea.dispatch("input");
            textarea.value = "/st";
            textarea.selectionStart = 3;
            textarea.dispatch("input");

            pending[1].resolve({ suggestions: [{ kind: "command", value: "/status", label: "status 状态" }] });
            await Promise.resolve();
            await Promise.resolve();
            pending[0].resolve({ suggestions: [{ kind: "command", value: "/compact", label: "compact 压缩" }] });
            await Promise.resolve();
            await Promise.resolve();

            const tokens = [];
            const visit = (node) => {
              if (String(node.className).split(/\\s+/).includes("suggestion-token")) tokens.push(node.textContent);
              for (const child of node.children || []) visit(child);
            };
            visit(suggestions);
            console.log(JSON.stringify({ tokens }));
            """
        ),
    )

    assert output == {"tokens": ["Status"]}


def test_composer_renders_selected_skill_as_chip_and_submits_hidden_token(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.className = "";
                this.textContent = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.dataset = {};
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, ...extra });
                }
              }
              focus() {}
              contains(target) {
                return target === this || this.children.includes(target);
              }
            }

            function textOf(node) {
              if (!node) {
                return "";
              }
              return [node.textContent || "", ...(node.children || []).map(textOf)].join("");
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
              addEventListener() {},
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const form = new Element("form");
            const textarea = new Element("textarea");
            const skillRow = new Element("div");
            let posted = null;
            const accepted = [];
            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                skillRow,
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                getSuggestions() {
                  return Promise.resolve({
                    suggestions: [
                      {
                        kind: "skill",
                        value: "$Brainstorming",
                        label: "Brainstorming Explore intent before implementation",
                        origin: "bundled",
                      },
                    ],
                  });
                },
                postCommand(sessionId, command) {
                  posted = { sessionId, command };
                  return Promise.resolve({ accepted: true });
                },
              },
              {
                onSubmitAccepted(event) {
                  accepted.push(event);
                },
              },
            );
            controller.setSession("S");
            textarea.value = "$Brain";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            await Promise.resolve();
            textarea.dispatch("keydown", { key: "Enter", shiftKey: false });
            const valueAfterSkill = textarea.value;
            const chipClass = skillRow.children[0]?.className || "";
            const chipText = textOf(skillRow);

            textarea.value = "创建一个 VPC";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            await controller.submit();

            console.log(JSON.stringify({
              valueAfterSkill,
              chipClass,
              chipText,
              posted,
              acceptedText: accepted[0]?.text || "",
            }));
            """
        ),
    )

    assert output == {
        "valueAfterSkill": "",
        "chipClass": "composer-skill-chip",
        "chipText": "Brainstorming",
        "posted": {
            "sessionId": "S",
            "command": "$Brainstorming\n创建一个 VPC",
        },
        "acceptedText": "$Brainstorming\n创建一个 VPC",
    }


def test_composer_allows_selected_skill_to_be_removed_with_backspace(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.className = "";
                this.textContent = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.dataset = {};
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                let prevented = false;
                for (const handler of this.listeners[type] || []) {
                  handler({
                    type,
                    target: this,
                    preventDefault() {
                      prevented = true;
                    },
                    ...extra,
                  });
                }
                return prevented;
              }
              focus() {}
              contains(target) {
                return target === this || this.children.includes(target);
              }
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
              addEventListener() {},
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const textarea = new Element("textarea");
            const skillRow = new Element("div");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea,
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                skillRow,
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                getSuggestions() {
                  return Promise.resolve({
                    suggestions: [
                      {
                        kind: "skill",
                        value: "$Brainstorming",
                        label: "Brainstorming Explore intent before implementation",
                        origin: "bundled",
                      },
                    ],
                  });
                },
              },
            );
            controller.setSession("S");
            textarea.value = "$Brain";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            await Promise.resolve();
            textarea.dispatch("keydown", { key: "Enter", shiftKey: false });

            const beforeBackspace = {
              hidden: skillRow.hidden,
              childCount: skillRow.children.length,
            };
            textarea.value = "";
            textarea.selectionStart = 0;
            const prevented = textarea.dispatch("keydown", { key: "Backspace" });

            console.log(JSON.stringify({
              beforeBackspace,
              prevented,
              hiddenAfterBackspace: skillRow.hidden,
              childCountAfterBackspace: skillRow.children.length,
            }));
            """
        ),
    )

    assert output == {
        "beforeBackspace": {"hidden": False, "childCount": 1},
        "prevented": True,
        "hiddenAfterBackspace": True,
        "childCountAfterBackspace": 0,
    }


def test_composer_accepts_status_suggestion_as_immediate_command(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.className = "";
                this.textContent = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.dataset = {};
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, ...extra });
                }
              }
              focus() {}
              contains(target) {
                return target === this || this.children.includes(target);
              }
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
              addEventListener() {},
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const textarea = new Element("textarea");
            let posted = null;
            const commandResults = [];
            const accepted = [];
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea,
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                skillRow: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                getSuggestions() {
                  return Promise.resolve({
                    suggestions: [{ kind: "command", value: "/status", label: "status 显示当前会话状态" }],
                  });
                },
                postCommand(sessionId, command) {
                  posted = { sessionId, command };
                  return Promise.resolve({ accepted: true, command: "status", status: { sessionId: "uuid" } });
                },
              },
              {
                onCommandResult(result) {
                  commandResults.push(result);
                },
                onSubmitAccepted(event) {
                  accepted.push(event);
                },
              },
            );
            controller.setSession("S");
            textarea.value = "/st";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            await Promise.resolve();
            textarea.dispatch("keydown", { key: "Enter", shiftKey: false });
            await Promise.resolve();

            console.log(JSON.stringify({
              valueAfterStatus: textarea.value,
              posted,
              commandResult: commandResults[0] || null,
              accepted: accepted[0] || null,
            }));
            """
        ),
    )

    assert output == {
        "valueAfterStatus": "",
        "posted": {"sessionId": "S", "command": "/status"},
        "commandResult": {"accepted": True, "command": "status", "status": {"sessionId": "uuid"}},
        "accepted": {
            "sessionId": "S",
            "kind": "command",
            "text": "/status",
            "result": {"accepted": True, "command": "status", "status": {"sessionId": "uuid"}},
        },
    }


def test_composer_materializes_draft_session_only_on_submit(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(element) {
                this.element = element;
              }
              toggle(name, force) {
                const classes = new Set(this.element.className.split(/\\s+/).filter(Boolean));
                if (force) {
                  classes.add(name);
                } else {
                  classes.delete(name);
                }
                this.element.className = [...classes].join(" ");
              }
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.dataset = {};
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, ...extra });
                }
              }
              focus() {}
              querySelector() {
                return null;
              }
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const form = new Element("form");
            const textarea = new Element("textarea");
            const sendButton = new Element("button");
            const createdPayloads = [];
            const postCalls = [];
            const submitted = [];

            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton,
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
              },
              {
                postMessage(sessionId, payload) {
                  postCalls.push({ sessionId, payload });
                  return Promise.resolve({ accepted: true });
                },
              },
              {
                createSessionForSubmit() {
                  createdPayloads.push("called");
                  // Production materializes the draft and immediately binds the composer to
                  // the newly created session before returning it.
                  controller.setSession("draft-web-session", { preserveDraft: true });
                  return Promise.resolve({ webSessionId: "draft-web-session" });
                },
                onSubmitAccepted(event) {
                  submitted.push(event.sessionId);
                },
              },
            );

            const initiallyDisabled = sendButton.disabled;
            textarea.value = "  deploy vpc  ";
            textarea.selectionStart = textarea.value.length;
            textarea.dispatch("input");
            const enabledWithDraftText = !sendButton.disabled;
            await controller.submit();

            console.log(JSON.stringify({
              initiallyDisabled,
              enabledWithDraftText,
              createdPayloads,
              postCalls,
              submitted,
              textareaValue: textarea.value,
              disabledAfterSubmit: sendButton.disabled,
            }));
            """
        ),
    )

    assert output == {
        "initiallyDisabled": True,
        "enabledWithDraftText": True,
        "createdPayloads": ["called"],
        "postCalls": [
            {
                "sessionId": "draft-web-session",
                "payload": {"text": "  deploy vpc  ", "imageIds": [], "fileRefs": []},
            }
        ],
        "submitted": ["draft-web-session"],
        "textareaValue": "",
        "disabledAfterSubmit": True,
    }

    app_source = _source(APP_JS)
    create_body = app_source.split("async function createSessionForSubmit()", 1)[1].split("\n}", 1)[0]
    assert "composer?.setSession(state.currentSessionId, { preserveDraft: true })" in create_body


def test_stale_draft_session_creation_cannot_replace_a_newer_session_selection() -> None:
    app_source = _source(APP_JS)
    create_body = app_source.split("async function createSessionForSubmit()", 1)[1].split("\n}", 1)[0]

    create_await = create_body.index("await api.createSession")
    stale_guard = create_body.index("generation !== sessionLoadGeneration")
    state_update = create_body.index("materializedDraftSession = session")
    assert create_await < stale_guard < state_update
    assert "await api.deleteSession(displaySessionId(session))" in create_body


def test_composer_rejects_unsupported_draft_image_before_creating_session(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) { this.owner = owner; this.items = new Set(); }
              toggle(name, force) {
                force ? this.items.add(name) : this.items.delete(name);
                this.owner.className = [...this.items].join(" ");
              }
            }
            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) { this.children.push(...children); }
              replaceChildren(...children) { this.children = children; }
              setAttribute(name, value) { this[name] = String(value); }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              querySelector() { return null; }
              querySelectorAll() { return []; }
              focus() {}
            }

            globalThis.URL = {
              createObjectURL() { return "blob:unsupported"; },
              revokeObjectURL() {},
            };
            globalThis.document = { createElement(tagName) { return new Element(tagName); } };
            globalThis.window = {
              getComputedStyle() { return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" }; },
            };

            let createCalls = 0;
            const errorTarget = new Element("output");
            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea: new Element("textarea"),
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget,
              },
              { getSuggestions() { return Promise.resolve({ suggestions: [] }); } },
              {
                createSessionForSubmit() {
                  createCalls += 1;
                  return Promise.resolve({ webSessionId: "should-not-exist" });
                },
              },
            );

            await controller.addFiles([{ name: "diagram.svg", type: "image/svg+xml" }]);
            await controller.submit();

            console.log(JSON.stringify({ createCalls, errorText: errorTarget.textContent }));
            """
        ),
    )

    assert output == {"createCalls": 0, "errorText": "Unsupported image type."}


def test_composer_keeps_image_thumbnail_when_upload_fails(tmp_path: Path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(element) {
                this.element = element;
              }
              toggle(name, force) {
                const classes = new Set(this.element.className.split(/\\s+/).filter(Boolean));
                if (force) {
                  classes.add(name);
                } else {
                  classes.delete(name);
                }
                this.element.className = [...classes].join(" ");
              }
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.src = "";
                this.alt = "";
                this.title = "";
                this.attributes = {};
                this.dataset = {};
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
                this.textContent = "";
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
                this[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type, extra = {}) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this, preventDefault() {}, ...extra });
                }
              }
              click() {
                this.dispatch("click");
              }
              querySelector(selector) {
                const className = selector.startsWith(".") ? selector.slice(1) : "";
                return this.children.find((child) => child.className.split(/\\s+/).includes(className)) || null;
              }
              focus() {}
            }

            const revoked = [];
            const previews = [];
            globalThis.URL = {
              createObjectURL(file) {
                return `blob:preview-${file.name}`;
              },
              revokeObjectURL(url) {
                revoked.push(url);
              },
            };
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const form = new Element("form");
            const textarea = new Element("textarea");
            const sendButton = new Element("button");
            const chips = new Element("div");
            const errorTarget = new Element("output");
            const controller = createComposerController(
              {
                form,
                textarea,
                sendButton,
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: chips,
                suggestions: new Element("div"),
                errorTarget,
              },
              {
                getSuggestions() {
                  return Promise.resolve({ suggestions: [] });
                },
                uploadImage() {
                  return Promise.reject(new Error("Current model qwen does not support image input."));
                },
              },
              {
                onPreviewImage(payload) {
                  previews.push(payload);
                },
              },
            );
            controller.setSession("S");

            await controller.addFiles([
              {
                name: "x.jpg",
                type: "image/jpeg",
                arrayBuffer() {
                  return Promise.resolve(new Uint8Array([1, 2, 3]).buffer);
                },
              },
            ]);

            const chip = chips.children[0];
            const previewBtn = chip.querySelector(".attachment-chip-preview-btn");
            const image = previewBtn?.children[0] || null;
            const remove = chip.querySelector(".attachment-chip-remove");
            const beforeRemove = {
              chipTagName: chip.tagName,
              chipClassName: chip.className,
              chipTitle: chip.title,
              imageSrc: image?.src || "",
              imageAlt: image?.alt || "",
              removeTagName: remove?.tagName || "",
              removeText: remove?.textContent || "",
              errorText: errorTarget.textContent,
              formClassName: form.className,
              sendDisabled: sendButton.disabled,
            };
            // 点缩略图应预览(不删除),点整块容器也不应删除;只有右上角 × 删除。
            previewBtn.click();
            chip.click();
            const chipCountAfterPreview = chips.children.length;
            remove.click();

            console.log(JSON.stringify({
              beforeRemove,
              previews,
              chipCountAfterPreview,
              chipCountAfterRemove: chips.children.length,
              revoked,
            }));
            """
        ),
    )

    assert output == {
        "beforeRemove": {
            "chipTagName": "DIV",
            "chipClassName": "attachment-chip attachment-chip-image is-failed",
            "chipTitle": "x.jpg · failed",
            "imageSrc": "blob:preview-x.jpg",
            "imageAlt": "x.jpg",
            "removeTagName": "BUTTON",
            "removeText": "×",
            "errorText": "Current model qwen does not support image input.",
            "formClassName": "has-attachments",
            "sendDisabled": False,
        },
        "previews": [{"src": "blob:preview-x.jpg", "alt": "x.jpg"}],
        "chipCountAfterPreview": 1,
        "chipCountAfterRemove": 0,
        "revoked": ["blob:preview-x.jpg"],
    }


def test_composer_model_provider_effort_menu_saves_active_provider(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                if (force) {
                  this.items.add(name);
                } else {
                  this.items.delete(name);
                }
                this.owner.className = [...this.items].join(" ");
              }
            }

            function dataKey(name) {
              return name
                .slice("data-".length)
                .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
            }

            function selectorMatches(node, selector) {
              const classMatch = selector.match(/^\\.([\\w-]+)$/);
              if (classMatch) {
                return String(node.className || "").split(/\\s+/).includes(classMatch[1]);
              }
              const dataMatch = selector.match(/^\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!dataMatch) {
                return false;
              }
              const [, dataName, expected] = dataMatch;
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            const documentListeners = {};

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
                if (name.startsWith("data-")) {
                  this.dataset[dataKey(name)] = String(value);
                }
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              contains(node) {
                if (node === this) {
                  return true;
                }
                return (this.children || []).some((child) => child?.contains?.(node));
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this, stopPropagation() {}, preventDefault() {} });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              focus() {}
            }

            function textOf(node) {
              const childText = (node.children || []).map(textOf).join(" ");
              return `${node.textContent || ""} ${childText}`.replace(/\\s+/g, " ").trim();
            }

            function required(root, selector) {
              const node = root.querySelector(selector);
              if (!node) {
                throw new Error(`missing ${selector}`);
              }
              return node;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
              addEventListener(type, handler) {
                documentListeners[type] = [...(documentListeners[type] || []), handler];
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const modelControl = new Element("button");
            const modelMenu = new Element("div");
            const saves = [];
            const api = {
              getProviders() {
                return Promise.resolve({
                  active: {
                    provider: "openai",
                    model: "gpt-5.5",
                    effort: "medium",
                    apiBase: "https://llm.example/v1",
                    hasApiKey: true,
                  },
                  providers: [
                    {
                      key: "openai",
                      name: "OpenAI",
                      hasApiKey: true,
                      configured: true,
                      models: [
                        { id: "gpt-5.5", name: "GPT-5.5", efforts: ["low", "medium", "high"] },
                        { id: "gpt-5-mini", name: "GPT-5 Mini", efforts: ["low"] },
                      ],
                    },
                    {
                      key: "anthropic",
                      name: "Anthropic",
                      defaultModel: "claude-sonnet-4",
                      hasApiKey: true,
                      configured: true,
                      models: [
                        { id: "claude-sonnet-4", name: "Claude Sonnet 4", efforts: ["medium", "high"] },
                      ],
                    },
                    {
                      key: "deepseek",
                      name: "DeepSeek",
                      hasApiKey: true,
                      configured: false,
                      models: [
                        { id: "deepseek-chat", name: "DeepSeek Chat", efforts: ["medium"] },
                      ],
                    },
                  ],
                });
              },
              saveSessionModel(sessionId, payload) {
                saves.push(payload);
                return Promise.resolve({ provider: payload.provider, model: payload.model, effort: payload.effort });
              },
            };

            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea: new Element("textarea"),
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
                modelControl,
                modelMenu,
              },
              api,
            );
            controller.setSession("sess-1");
            await new Promise((resolve) => setTimeout(resolve, 0));

            const initialControl = textOf(modelControl);
            modelControl.click();
            const opened = !modelMenu.hidden;
            const openedMenuText = textOf(modelMenu);
            required(modelMenu, '[data-composer-setting="effort:high"]').click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            required(modelMenu, '[data-composer-submenu-trigger="model"]').click();
            const modelSubmenuVisible = !required(modelMenu, '[data-composer-submenu="model"]').hidden;
            const modelSubmenuText = textOf(required(modelMenu, '[data-composer-submenu="model"]'));
            required(modelMenu, '[data-composer-setting="model:gpt-5-mini"]').click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            required(modelMenu, '[data-composer-submenu-trigger="provider"]').click();
            const providerSubmenuVisible = !required(modelMenu, '[data-composer-submenu="provider"]').hidden;
            const providerSubmenuText = textOf(required(modelMenu, '[data-composer-submenu="provider"]'));
            required(modelMenu, '[data-composer-setting="provider:anthropic"]').click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const stillOpenBeforeOutsideClick = !modelMenu.hidden;
            for (const handler of documentListeners.click || []) {
              handler({ type: "click", target: new Element("main") });
            }
            const closedAfterOutsideClick = modelMenu.hidden;

            console.log(JSON.stringify({
              initialControl,
              opened,
              menuText: openedMenuText,
              modelSubmenuVisible,
              modelSubmenuText,
              providerSubmenuVisible,
              providerSubmenuText,
              stillOpenBeforeOutsideClick,
              closedAfterOutsideClick,
              saves,
              finalControl: textOf(modelControl),
            }));
            """
        ),
    )

    assert output["initialControl"] == "GPT-5.5 Medium"
    assert output["opened"] is True
    assert "Reasoning" in output["menuText"]
    assert "High" in output["menuText"]
    assert "GPT-5.5" in output["menuText"]
    assert "OpenAI" in output["menuText"]
    assert "GPT-5 Mini" not in output["menuText"]
    assert "Anthropic" not in output["menuText"]
    assert output["modelSubmenuVisible"] is True
    assert "GPT-5 Mini" in output["modelSubmenuText"]
    assert output["providerSubmenuVisible"] is True
    assert "Anthropic" in output["providerSubmenuText"]
    assert "DeepSeek" not in output["providerSubmenuText"]
    assert output["stillOpenBeforeOutsideClick"] is True
    assert output["closedAfterOutsideClick"] is True
    assert output["saves"] == [
        {"provider": "openai", "model": "gpt-5.5", "effort": "high"},
        {"provider": "openai", "model": "gpt-5-mini", "effort": "low"},
        {"provider": "anthropic", "model": "claude-sonnet-4", "effort": "medium"},
    ]
    assert output["finalControl"] == "Claude Sonnet 4 Medium"


def test_composer_permission_mode_menu_saves_session_mode(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { createComposerController } = await import(__COMPOSER_MODULE__);

            class ClassList {
              constructor(owner) {
                this.owner = owner;
                this.items = new Set();
              }
              toggle(name, force) {
                if (force) {
                  this.items.add(name);
                } else {
                  this.items.delete(name);
                }
                this.owner.className = [...this.items].join(" ");
              }
            }

            function dataKey(name) {
              return name
                .slice("data-".length)
                .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
            }

            function selectorMatches(node, selector) {
              const classMatch = selector.match(/^\\.([\\w-]+)$/);
              if (classMatch) {
                return String(node.className || "").split(/\\s+/).includes(classMatch[1]);
              }
              const dataMatch = selector.match(/^\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!dataMatch) {
                return false;
              }
              const [, dataName, expected] = dataMatch;
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            const documentListeners = {};

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.disabled = false;
                this.hidden = false;
                this.selectionStart = 0;
                this.scrollTop = 0;
                this.clientHeight = 120;
                this.classList = new ClassList(this);
                this.style = { setProperty() {} };
              }
              append(...children) {
                this.children.push(...children);
              }
              appendChild(child) {
                this.children.push(child);
                return child;
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
                if (name.startsWith("data-")) {
                  this.dataset[dataKey(name)] = String(value);
                }
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              contains(node) {
                if (node === this) {
                  return true;
                }
                return (this.children || []).some((child) => child?.contains?.(node));
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this, stopPropagation() {}, preventDefault() {} });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              focus() {}
            }

            function textOf(node) {
              const childText = (node.children || []).map(textOf).join(" ");
              return `${node.textContent || ""} ${childText}`.replace(/\\s+/g, " ").trim();
            }

            function required(root, selector) {
              const node = root.querySelector(selector);
              if (!node) {
                throw new Error(`missing ${selector}`);
              }
              return node;
            }

            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
              createElementNS(_namespace, tagName) {
                return new Element(tagName);
              },
              addEventListener(type, handler) {
                documentListeners[type] = [...(documentListeners[type] || []), handler];
              },
            };
            globalThis.window = {
              getComputedStyle() {
                return { lineHeight: "20px", paddingTop: "12px", paddingLeft: "12px" };
              },
            };

            const permissionControl = new Element("button");
            const permissionMenu = new Element("div");
            const saves = [];
            const api = {
              savePermissionMode(sessionId, mode) {
                saves.push([sessionId, mode]);
                return Promise.resolve({ permissionMode: mode });
              },
            };

            const controller = createComposerController(
              {
                form: new Element("form"),
                textarea: new Element("textarea"),
                sendButton: new Element("button"),
                stopButton: new Element("button"),
                interruptButton: new Element("button"),
                fileInput: new Element("input"),
                attachmentChips: new Element("div"),
                suggestions: new Element("div"),
                errorTarget: new Element("output"),
                permissionControl,
                permissionMenu,
              },
              api,
            );
            controller.setSession("S");
            controller.setPermissionMode("default");
            const initialControl = textOf(permissionControl);

            permissionControl.click();
            const opened = !permissionMenu.hidden;
            const openedMenuText = textOf(permissionMenu);
            required(permissionMenu, '[data-permission-mode="accept_edits"]').click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const closedAfterSave = permissionMenu.hidden;
            const savedControl = textOf(permissionControl);
            const savedClass = permissionControl.className;

            permissionControl.click();
            const reopened = !permissionMenu.hidden;
            for (const handler of documentListeners.click || []) {
              handler({ type: "click", target: new Element("main") });
            }
            const closedAfterOutsideClick = permissionMenu.hidden;

            console.log(JSON.stringify({
              initialControl,
              opened,
              openedMenuText,
              closedAfterSave,
              savedControl,
              savedClass,
              reopened,
              closedAfterOutsideClick,
              saves,
            }));
            """
        ),
    )

    assert output["initialControl"] == "Ask for approval"
    assert output["opened"] is True
    assert "How should IaC Code actions be approved?" in output["openedMenuText"]
    assert "Ask for approval" in output["openedMenuText"]
    assert "Approve for me" in output["openedMenuText"]
    assert "Automatically accept common file edits; still ask for other risky actions" in output["openedMenuText"]
    assert "Full access permissions" in output["openedMenuText"]
    assert "Don't ask" in output["openedMenuText"]
    assert "Automatically deny actions that need approval" in output["openedMenuText"]
    assert "了解更多" not in output["openedMenuText"]
    assert output["closedAfterSave"] is True
    assert output["savedControl"] == "Approve for me"
    assert "is-accept-edits" in output["savedClass"]
    assert output["reopened"] is True
    assert output["closedAfterOutsideClick"] is True
    assert output["saves"] == [["S", "accept_edits"]]


def test_composer_suggestions_are_overlayed_inside_input_and_compact() -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)
    suggestions_block = _css_block(styles, ".transcript-panel .suggestions")
    hidden_suggestions_block = _css_block(styles, ".transcript-panel .suggestions[hidden]")
    item_block = _css_block(styles, ".transcript-panel .suggestion-item")
    skill_item_block = _css_block(styles, ".transcript-panel .suggestion-item.is-skill-suggestion")
    section_label_block = _css_block(styles, ".suggestion-section-label")
    command_copy_block = _css_block(styles, ".suggestion-item.is-command-suggestion .suggestion-copy")
    skill_copy_block = _css_block(styles, ".suggestion-item.is-skill-suggestion .suggestion-copy")
    scope_block = _css_block(styles, ".suggestion-scope")
    icon_block = _css_block(styles, ".suggestion-icon")
    active_block = _css_block(styles, ".transcript-panel .suggestion-item.is-active")
    status_icon_block = _css_block(styles, ".suggestion-icon.is-command-status::before")

    assert "composer-input-wrap" in html
    assert html.index('class="composer-input-wrap"') < html.index('data-app-shell="suggestions"')
    assert ".composer-input-wrap" in styles
    assert "position: relative" in styles
    assert ".suggestions" in styles
    assert "position: absolute" in styles
    assert "bottom: calc(100% +" in styles
    assert "z-index:" in styles
    assert "left: 0;" in suggestions_block
    assert "right: 0;" in suggestions_block
    assert "max-height: min(32rem, calc(100vh - 12.8rem));" in suggestions_block
    assert "border-radius: 22px;" in suggestions_block
    assert "background: color-mix(in srgb, var(--codex-panel-raised) 94%, transparent);" in suggestions_block
    assert "backdrop-filter: blur(24px);" in suggestions_block
    assert "scrollbar-width: none;" in suggestions_block
    assert "display: none;" in hidden_suggestions_block
    assert "grid-template-columns: 1.08rem minmax(0, 1fr) auto;" in item_block
    assert "min-height: 2.14rem;" in item_block
    assert "font-size: 0.88rem;" in item_block
    assert "grid-template-columns: minmax(3.35rem, max-content) minmax(0, 1fr);" in command_copy_block
    assert "display: flex;" in skill_copy_block
    assert "grid-template-columns: 1.08rem minmax(0, 1fr) auto;" in skill_item_block
    assert "font-size: 0.82rem;" in section_label_block
    assert "color: var(--codex-muted);" in section_label_block
    assert "justify-self: end;" in scope_block
    assert "border-radius: 12px;" in active_block
    assert "background: color-mix(in srgb, var(--codex-ink) 12%, transparent);" in active_block
    assert "width: 1rem;" in icon_block
    assert ".suggestion-icon.is-command-compact::before" in styles
    assert ".suggestion-icon.is-command-effort::before" in styles
    assert ".suggestion-icon.is-skill::before" in styles
    assert "M3.34 19a10 10 0 1 1 17.32 0" in status_icon_block
    assert "M5 15.2a7 7 0 0 1 14 0" not in status_icon_block
    assert "M4 17.5a8 8 0 1 1 16 0" not in status_icon_block


def test_composer_exposes_codex_like_model_picker_and_disabled_send_styles() -> None:
    styles = _source(STYLES_CSS)

    actions_block = _css_block(styles, ".composer-actions")
    picker_block = _css_block(styles, ".composer-model-picker")
    control_block = _css_block(styles, ".composer-model-control")
    control_hover_block = _css_block(
        styles,
        ".composer-model-control:hover,\n"
        ".composer-model-control:focus-visible,\n"
        '.composer-model-control[aria-expanded="true"]',
    )
    menu_block = _css_block(styles, ".composer-model-menu")
    submenu_block = _css_block(styles, ".composer-model-submenu")
    send_disabled_block = _css_block(styles, ".send-action:disabled")
    dark_menu_block = _css_block(styles, ".transcript-panel .composer-model-menu")
    dark_control_hover_block = _css_block(
        styles,
        ".transcript-panel .composer-model-control:hover,\n"
        ".transcript-panel .composer-model-control:focus-visible,\n"
        '.transcript-panel .composer-model-control[aria-expanded="true"]',
    )

    assert "justify-content: flex-end;" in actions_block
    assert "margin-left: 0;" in picker_block
    assert "border-radius: 999px;" in control_block
    assert "border-color: transparent;" in control_block
    assert "background: transparent;" in control_block
    assert "outline: 0;" in control_block
    assert "font-weight: 450;" in control_block
    assert "border-color: transparent;" in control_hover_block
    assert "border-color: transparent;" in dark_control_hover_block
    assert 'content: "⌄";' not in styles
    assert "border-right: 1.5px solid currentColor;" in _css_block(styles, ".composer-model-control::after")
    assert "bottom: calc(100% + 0.5rem);" in menu_block
    assert "border-radius: 16px;" in menu_block
    assert "grid-template-columns: minmax(0, 1fr) 1rem;" in _css_block(styles, ".composer-model-menu-item")
    assert "font-weight: 400;" in _css_block(styles, ".composer-model-menu-item")
    assert ".composer-model-menu-item.is-active:not(.composer-model-submenu-trigger)::after" in styles
    assert ".composer-model-submenu-trigger.is-active::after" not in styles
    assert "border-right: 1.5px solid currentColor;" in _css_block(styles, ".composer-model-menu-chevron")
    assert "left: calc(100% + 0.34rem);" in submenu_block
    assert "background: rgba(32, 32, 29, 0.15);" in send_disabled_block
    assert "opacity: 1;" in send_disabled_block
    assert "background: color-mix(in srgb, var(--codex-panel-raised) 94%, transparent);" in dark_menu_block
    assert "backdrop-filter: blur(24px);" in dark_menu_block


def test_composer_exposes_codex_like_permission_mode_picker() -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)
    composer_source = _source(COMPOSER_JS)

    assert 'data-app-shell="permission-mode-control"' in html
    assert 'data-app-shell="permission-mode-menu"' in html
    assert html.index('class="file-action"') < html.index('data-app-shell="permission-mode-control"')
    assert html.index('data-app-shell="permission-mode-control"') < html.index(
        'data-app-shell="composer-model-control"'
    )

    picker_block = _css_block(styles, ".permission-mode-picker")
    control_block = _css_block(styles, ".permission-mode-control")
    control_hover_block = _css_block(
        styles,
        ".permission-mode-control:hover,\n"
        ".permission-mode-control:focus-visible,\n"
        '.permission-mode-control[aria-expanded="true"]',
    )
    menu_block = _css_block(styles, ".permission-mode-menu")
    header_block = _css_block(styles, ".permission-mode-menu-header")
    item_block = _css_block(styles, ".permission-mode-menu-item")
    icon_block = _css_block(styles, ".permission-mode-icon")
    label_block = _css_block(styles, ".permission-mode-menu-label")
    description_block = _css_block(styles, ".permission-mode-menu-description")
    full_access_block = _css_block(styles, ".permission-mode-control.is-bypass-permissions")
    accept_edits_block = _css_block(styles, ".permission-mode-control.is-accept-edits")
    dark_menu_block = _css_block(styles, ".transcript-panel .permission-mode-menu")
    dark_control_hover_block = _css_block(
        styles,
        ".transcript-panel .permission-mode-control:hover,\n"
        ".transcript-panel .permission-mode-control:focus-visible,\n"
        '.transcript-panel .permission-mode-control[aria-expanded="true"]',
    )

    assert "order: 2;" in picker_block
    assert "margin-right: auto;" in picker_block
    assert "border-color: transparent;" in control_block
    assert "background: transparent;" in control_block
    assert "outline: 0;" in control_block
    assert "font-weight: 450;" in control_block
    assert "border-color: transparent;" in control_hover_block
    assert "border-color: transparent;" in dark_control_hover_block
    assert "bottom: calc(100% + 0.5rem);" in menu_block
    assert "border-radius: 16px;" in menu_block
    assert "font-weight: 500;" in header_block
    assert "grid-template-columns: 1.4rem minmax(0, 1fr) 1rem;" in item_block
    assert "min-height: 3.18rem;" in item_block
    assert "font-size: 0.9rem;" in item_block
    assert "stroke-width: 1.7;" in icon_block
    assert "font-weight: 400;" in label_block
    assert "font-size: 0.78rem;" in description_block
    assert 'icon: "terminal-shield"' in composer_source
    assert 'icon: "alert-shield"' in composer_source
    assert '"terminal-shield": [' in composer_source
    assert '"alert-shield": [' in composer_source
    assert "M10 3.1l5.2 2.1v4.2" in composer_source
    assert 'icon: "shield-check"' not in composer_source
    assert 'icon: "shield",' not in composer_source
    assert 'icon: "terminal-box"' not in composer_source
    assert 'icon: "alert-box"' not in composer_source
    assert ".permission-mode-menu-link" not in styles
    assert "color: #ff8a3d;" in full_access_block
    assert "color: #7cc2ff;" in accept_edits_block
    assert "background: color-mix(in srgb, var(--codex-panel-raised) 94%, transparent);" in dark_menu_block
    assert "backdrop-filter: blur(24px);" in dark_menu_block


def test_transcript_hides_scrollbars_and_wraps_markdown_content() -> None:
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    stack_block = _css_block(styles, ".transcript-panel .message-stack")
    overflowing_stack_block = _css_block(styles, ".transcript-panel .message-stack.is-overflowing")
    empty_stack_block = _css_block(styles, ".transcript-panel:has(.message-empty) .message-stack")
    composer_block = _css_block(styles, ".transcript-panel .composer")
    stack_scrollbar_block = _css_block(styles, ".transcript-panel .message-stack::-webkit-scrollbar")
    empty_blocking_block = _css_block(styles, ".blocking-region:not(:has(.blocking-panel))")
    active_blocking_block = _css_block(styles, ".blocking-region:has(.blocking-panel)")
    markdown_pre_block = _css_block(styles, ".markdown-body pre")
    thinking_pre_block = _css_block(styles, ".message-thinking pre")
    table_block = _css_block(styles, ".markdown-body table")
    cell_block = _css_block(styles, ".markdown-body th,\n.markdown-body td")
    anchor_block = _css_block(styles, ".markdown-body a")
    fallback_tools_block = _css_block(styles, ".transcript-panel .tool-activity-region")

    assert "overflow-y: auto;" in stack_block
    assert "overflow-x: hidden;" in stack_block
    assert "scrollbar-width: none;" in stack_block
    assert "flex: 1 1 auto;" in stack_block
    assert "max-height: none;" in stack_block
    assert "align-content: start;" in stack_block
    assert "align-content: start;" in overflowing_stack_block
    assert "align-content: start;" in empty_stack_block
    # 空/新会话:消息区取消 flex-grow,收到标题高度而非撑满(否则输入框被顶回底部)。
    assert "flex: 0 1 auto;" in empty_stack_block
    assert "margin-top: auto;" in composer_block
    assert "flex: 0 0 auto;" in composer_block
    assert "display: none;" in stack_scrollbar_block
    assert "padding-bottom: 0;" in empty_blocking_block
    assert "padding-bottom: 1rem;" in active_blocking_block
    assert 'setElementClassFlag(stack, "is-overflowing", true);' in app_source
    assert 'setElementClassFlag(stack, "is-overflowing", isOverflowing);' in app_source
    for block in [markdown_pre_block, thinking_pre_block]:
        assert "overflow: hidden;" in block
        assert "white-space: pre-wrap;" in block
        assert "overflow-wrap: anywhere;" in block
        # 回归:pre 走「换行 + 页面滚动」,不得设裁剪高度。曾经思考块用 max-height:18rem +
        # overflow:hidden,展开长思考只看得到一部分且无法滚动(超高部分被裁、页面也够不到)。
        assert "max-height" not in block
    assert "table-layout: fixed;" in table_block
    assert "max-width: 100%;" in table_block
    assert "overflow-wrap: anywhere;" in cell_block
    assert "word-break: break-word;" in cell_block
    assert "overflow-wrap: anywhere;" in anchor_block
    assert "flex: 0 1 auto;" in fallback_tools_block
    assert "min-height: 0;" in fallback_tools_block
    assert "max-height: min(50vh, 28rem);" in fallback_tools_block
    assert "overflow-y: auto;" in fallback_tools_block


def test_empty_session_composer_sits_below_heading_not_pinned_bottom() -> None:
    # 新/空会话:输入框上移到标题正下方,而非钉在页面底部。机制是 flex 列里默认
    # .composer{margin-top:auto} 吸走上方空白把输入框推到底,且 message-stack{flex:1 1 auto}
    # 撑满;空状态两条同时松开——composer margin-top 归零 + message-stack 不再 grow——
    # 弹性空白改由 justify-content:flex-start 收到最底,输入框落在标题块下方。水平方向不变。
    styles = _source(STYLES_CSS)

    empty_composer_block = _css_block(styles, ".transcript-panel:has(.message-empty) .composer")
    empty_stack_block = _css_block(styles, ".transcript-panel:has(.message-empty) .message-stack")

    assert "margin-top: 0;" in empty_composer_block
    assert "flex: 0 1 auto;" in empty_stack_block
    # 水平方向不动:空状态不覆写 composer 的宽度/左右外边距(仍走 .transcript-panel .composer)。
    assert "margin-left" not in empty_composer_block
    assert "width" not in empty_composer_block


def test_composer_aligns_with_transcript_column_and_resizes_for_attachments() -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)
    composer_source = _source(COMPOSER_JS)

    input_wrap_index = html.index('class="composer-input-wrap"')
    attachment_index = html.index('data-app-shell="attachment-chips"')
    textarea_index = html.index('data-app-shell="composer-input"')
    assert input_wrap_index < attachment_index < textarea_index

    composer_block = _css_block(styles, ".transcript-panel .composer")
    empty_block = _css_block(styles, ".transcript-panel .message-empty")
    actions_block = _css_block(styles, ".composer-actions")
    input_wrap_block = _css_block(styles, ".transcript-panel .composer-input-wrap")
    attachment_height_block = _css_block(styles, ".composer.has-attachments .composer-input-wrap")
    image_chip_block = _css_block(styles, ".attachment-chip-image")
    preview_block = _css_block(styles, ".attachment-chip-preview")
    remove_block = _css_block(styles, ".attachment-chip-remove")
    transcript_image_chip_block = _css_block(styles, ".transcript-panel .attachment-chip-image")
    send_block = _css_block(styles, ".transcript-panel .send-action")
    file_block = _css_block(styles, ".transcript-panel .file-action")

    assert "width: min(40rem, calc(100% - 2rem));" in composer_block
    assert "margin-left: max(1rem, calc((100% - 820px) / 2));" in composer_block
    assert "margin-right: auto;" in composer_block
    assert "width: min(40rem, calc(100% - 2rem));" in empty_block
    assert "margin-left: 0;" in empty_block
    assert "margin-right: auto;" in empty_block
    assert "justify-items: center;" in empty_block
    assert "right: 0.48rem;" in actions_block
    assert "bottom: 0.52rem;" in actions_block
    assert "left: 0.48rem;" in actions_block
    assert "min-height: 6.25rem;" in input_wrap_block
    assert "padding: 0.78rem 0.88rem 2.9rem;" in input_wrap_block
    assert "min-height: 8.8rem;" in attachment_height_block
    assert "width: 5.25rem;" in image_chip_block
    assert "height: 5.25rem;" in image_chip_block
    assert "position: relative;" in image_chip_block
    assert "object-fit: cover;" in preview_block
    assert "position: absolute;" in remove_block
    assert "border-radius: 999px;" in remove_block
    assert "width: 5.25rem;" in transcript_image_chip_block
    assert "padding: 0;" in transcript_image_chip_block
    assert "width: 1.92rem;" in send_block
    assert "height: 1.92rem;" in send_block
    assert "border-color: transparent;" in send_block
    assert "width: 1.84rem;" in file_block
    assert "height: 1.84rem;" in file_block
    assert "border-color: transparent;" in file_block
    assert "background: transparent;" in file_block
    assert "syncComposerAttachmentState" in composer_source
    assert 'form?.classList?.toggle("has-attachments", attachments.length > 0)' in composer_source
    assert 'form?.classList?.toggle("has-skill", Boolean(selectedSkill))' in composer_source


def test_composer_uses_line_icons_for_file_send_and_model_controls() -> None:
    html = _source(INDEX_HTML)
    styles = _source(STYLES_CSS)
    composer_source = _source(COMPOSER_JS)
    file_icon_block = _css_block(styles, ".file-action .composer-icon")

    assert 'class="composer-icon composer-icon-plus"' in html
    assert 'class="composer-icon composer-icon-send"' in html
    assert '<path d="M10 3V17" />' in html
    assert '<path d="M3 10H17" />' in html
    assert '<path d="M10 3.75V16.25" />' not in html
    assert '<path d="M3.75 10H16.25" />' not in html
    assert '<path d="M10 4.75V15.25" />' not in html
    assert '<path d="M4.75 10H15.25" />' not in html
    assert "width: 1.14rem;" in file_icon_block
    assert "height: 1.14rem;" in file_icon_block
    assert '<label class="file-action">\n                +' not in html
    assert 'aria-hidden="true"' in html
    assert ".composer-icon" in styles
    assert "stroke-linecap: round;" in styles
    assert 'chevron.textContent = "›";' not in composer_source
    assert 'chevron.setAttribute("aria-hidden", "true")' in composer_source


def test_mobile_suggestions_float_above_composer_actions() -> None:
    styles = _source(STYLES_CSS)

    assert "@media (max-width: 760px)" in styles
    assert "bottom: calc(100% + 0.45rem)" in styles
    assert "max-height: min(11rem, 36vh)" in styles


def test_app_renders_structured_message_content_without_object_object(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { messageText } = await import(__APP_MODULE__);
            const rendered = messageText({
              content: [
                { type: "text", text: "assistant text" },
                { type: "tool_result", content: [{ type: "text", text: "nested result" }] },
                { type: "json", input: { Resource: { Type: "ALIYUN::ECS::VPC" } } },
              ],
            });

            console.log(JSON.stringify({
              rendered,
              hasObjectObject: rendered.includes("[object Object]"),
            }));
            """
        ),
    )

    assert output["hasObjectObject"] is False
    assert "assistant text" in output["rendered"]
    assert "nested result" in output["rendered"]
    assert "ALIYUN::ECS::VPC" in output["rendered"]


def test_derive_context_fallback_label_names_pipeline_step(tmp_path) -> None:
    # 无活跃步骤窗口时 composer 回退到单主环。普通会话该环标「普通会话」；流水线会话此刻并非普通对话
    # (选择门/步骤间隙/reload,用量不持久化),回退标签必须改为「等待步骤名 / 流水线名」而非「普通会话」。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById() { return null; } };
            const { deriveContextFallbackLabel } = await import(__APP_MODULE__);

            // 普通会话 → 空串(composer 沿用默认「普通会话」标签)。
            const normal = deriveContextFallbackLabel({ messages: {} }, { mode: "normal" });

            // 流水线会话停在选择门:某步骤 marker 以 status="input" 重发,取其步骤名。
            const atGate = deriveContextFallbackLabel(
              {
                messages: {
                  "plmk-1": { sequence: 3, pipelineStep: { status: "completed", title: "Generate" } },
                  "plmk-2": { sequence: 7, pipelineStep: { status: "input", title: "Confirm & select" } },
                },
              },
              { mode: "pipeline", pipelineName: "selling" },
            );

            // 流水线会话步骤间隙(无 input marker)→ 退到流水线名。
            const betweenSteps = deriveContextFallbackLabel(
              { messages: { "plmk-1": { sequence: 1, pipelineStep: { status: "completed", title: "Generate" } } } },
              { mode: "pipeline", pipelineName: "selling" },
            );

            console.log(JSON.stringify({ normal, atGate, betweenSteps }));
            """
        ),
    )

    assert output == {
        "normal": "",
        "atGate": "Confirm & select",
        "betweenSteps": "selling",
    }


def test_sidebar_mode_icon_stays_pipeline_after_handoff_to_normal(tmp_path) -> None:
    # 流水线交接给普通对话后 session.mode 落为 "normal",但 sidecar 仍保留 contextId/taskId
    # (与 load_visible_transcript 的 reload 回放同一套「曾是流水线」解耦)。侧栏图标必须仍显示
    # 流水线图标,而非因模式翻转而变成普通对话图标。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById() { return null; } };
            const { sessionModeIconClass } = await import(__APP_MODULE__);
            console.log(JSON.stringify({
              pipelineMode: sessionModeIconClass({ mode: "pipeline" }),
              handedOffWithContext: sessionModeIconClass({ mode: "normal", contextId: "ctx-d190b8501a74" }),
              handedOffWithTask: sessionModeIconClass({ mode: "normal", taskId: "task-1" }),
              pureNormal: sessionModeIconClass({ mode: "normal" }),
            }));
            """
        ),
    )

    assert output["pipelineMode"] == "is-pipeline-mode"
    assert output["handedOffWithContext"] == "is-pipeline-mode"
    assert output["handedOffWithTask"] == "is-pipeline-mode"
    assert output["pureNormal"] == "is-normal-mode"


def test_is_pipeline_transcript_detects_pipeline_and_handoff(tmp_path) -> None:
    # 流水线转录判定:live 时 mode==="pipeline";交接普通对话后 mode 翻转但 sidecar 仍留
    # contextId/taskId;草稿态用 newSessionDraft.mode。据此决定「只展开 complete_step、其余收起」。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById() { return null; } };
            const { isPipelineTranscript } = await import(__APP_MODULE__);
            console.log(JSON.stringify({
              pipelineMode: isPipelineTranscript({ currentSession: { mode: "pipeline" } }),
              handedOffCtx: isPipelineTranscript({ currentSession: { mode: "normal", contextId: "ctx-1" } }),
              handedOffTask: isPipelineTranscript({ currentSession: { mode: "normal", taskId: "task-1" } }),
              draftPipeline: isPipelineTranscript({ currentSession: {}, newSessionDraft: { mode: "pipeline" } }),
              pureNormal: isPipelineTranscript({ currentSession: { mode: "normal" } }),
              empty: isPipelineTranscript({}),
            }));
            """
        ),
    )

    assert output["pipelineMode"] is True
    assert output["handedOffCtx"] is True
    assert output["handedOffTask"] is True
    assert output["draftPipeline"] is True
    assert output["pureNormal"] is False
    assert output["empty"] is False


def test_normalize_session_defaults_clamps_each_field(tmp_path) -> None:
    # 权限空→default、模式仅 pipeline/normal、流水线空→缺省 selling;首屏注入与保存回写共用此规范化。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById() { return null; } };
            const { normalizeSessionDefaults } = await import(__APP_MODULE__);
            console.log(JSON.stringify({
              empty: normalizeSessionDefaults({}),
              full: normalizeSessionDefaults({ permissionMode: "acceptEdits", mode: "pipeline", pipelineName: "x" }),
              weirdMode: normalizeSessionDefaults({ mode: "weird" }),
              blank: normalizeSessionDefaults({ permissionMode: "  ", pipelineName: "  " }),
            }));
            """
        ),
    )

    assert output["empty"] == {"permissionMode": "default", "mode": "normal", "pipelineName": "selling"}
    assert output["full"] == {"permissionMode": "acceptEdits", "mode": "pipeline", "pipelineName": "x"}
    assert output["weirdMode"]["mode"] == "normal"
    assert output["blank"] == {"permissionMode": "default", "mode": "normal", "pipelineName": "selling"}


def test_apply_session_defaults_feeds_new_session_draft_without_reload(tmp_path) -> None:
    # 保存新默认后 applySessionDefaults 更新内存常量;无草稿时 makeNewSessionDraft 立即回落到新默认,
    # 无需刷新页面(修复「切换默认后返回新建会话不生效」)。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById() { return null; } };
            const { applySessionDefaults, makeNewSessionDraft } = await import(__APP_MODULE__);
            applySessionDefaults({ permissionMode: "acceptEdits", mode: "pipeline", pipelineName: "selling" });
            const draft = makeNewSessionDraft();
            console.log(JSON.stringify({
              mode: draft.mode,
              permissionMode: draft.permissionMode,
              pipelineName: draft.pipelineName,
            }));
            """
        ),
    )

    assert output["mode"] == "pipeline"
    assert output["permissionMode"] == "acceptEdits"
    assert output["pipelineName"] == "selling"


def test_should_open_tool_card_collapses_non_complete_step_in_pipeline(tmp_path) -> None:
    # 流水线(collapseNonComplete=true):所有工具(含 complete_step)默认收起,进行中/最新的过程
    # 工具也收起,消除逐条事件到达时的展开/收起闪烁。非流水线维持原样(complete_step/进行中/
    # 最新展开,Issue 2/3)。
    output = _run_toolcards_script(
        tmp_path,
        textwrap.dedent(
            """
            const { shouldOpenToolCard } = await import(__TOOLCARDS_MODULE__);
            console.log(JSON.stringify({
              pipelineComplete: shouldOpenToolCard({ isCompleteStep: true, collapseNonComplete: true }),
              pipelineInProgress: shouldOpenToolCard({ collapseNonComplete: true, inProgress: true }),
              pipelineLatest: shouldOpenToolCard({ collapseNonComplete: true, isLatest: true }),
              pipelineIdle: shouldOpenToolCard({ collapseNonComplete: true }),
              normalComplete: shouldOpenToolCard({ isCompleteStep: true }),
              normalInProgress: shouldOpenToolCard({ inProgress: true }),
              normalInProgressLatest: shouldOpenToolCard({ inProgress: true, isLatest: true }),
              normalLatest: shouldOpenToolCard({ isLatest: true }),
              normalIdle: shouldOpenToolCard({}),
            }));
            """
        ),
    )

    assert output["pipelineComplete"] is False
    assert output["pipelineInProgress"] is False
    assert output["pipelineLatest"] is False
    assert output["pipelineIdle"] is False
    assert output["normalComplete"] is True
    # 进行中的工具一律默认收起(Phase 4 Issue 1),即便它同时是尾部最新卡。
    assert output["normalInProgress"] is False
    assert output["normalInProgressLatest"] is False
    assert output["normalLatest"] is True
    assert output["normalIdle"] is False


def test_complete_step_tool_card_no_longer_force_opens() -> None:
    # complete_step 不再强制常开(去掉 forceOpen);流水线下默认收起,用户可手动展开,
    # 其展开态经 applyDetailsOpenOverrides 跨帧保留(openKey 仍在)。
    tool_cards = _source(TOOL_CARDS_JS)
    assert 'card.dataset.forceOpen = "1";' not in tool_cards
    # collapseNonComplete 短路在最前:流水线下含 complete_step 一律收起。
    body = tool_cards.split("export function shouldOpenToolCard(", 1)[1].split("\n}", 1)[0]
    assert body.index("if (collapseNonComplete)") < body.index("if (isCompleteStep)")
    # 进行中拦截必须在 isLatest(最终 return)之前,否则尾部进行中卡仍会因 isLatest 展开。
    assert "if (inProgress)" in body
    assert body.index("if (inProgress)") < body.index("return Boolean(isLatest)")
    # 2a 自动展开:hasActiveStackProgress 强制展开必须先于 collapseNonComplete/inProgress 的收起短路,
    # 否则部署进行中的进度卡仍被流水线收起,看不到实时进度。
    assert "hasActiveStackProgress" in body
    assert body.index("if (hasActiveStackProgress)") < body.index("if (collapseNonComplete)")
    assert body.index("if (hasActiveStackProgress)") < body.index("if (inProgress)")


def test_pipeline_thinking_indicator_is_injected_per_working_step(tmp_path) -> None:
    # 「正在思考」不再挂在整栈尾部,而是由单一事实源 syncPipelineThinking 给每个进行中
    # (data-step-status="working")的叶子步骤在其 body 内各维护一枚(支持多步并行);父步骤
    # 让位给进行中的子步骤(叶子优先),步骤内已有实时活动时撤除。render 快照与心跳共用此函数。
    app_source = _source(APP_JS)
    # 步骤 body 出口自描述状态,让心跳在静默期能从 live DOM 定位「进行中叶子」并取稳定键。
    assert "body.dataset.stepStatus = status;" in app_source
    assert "body.dataset.stepKey = details.dataset.openKey;" in app_source
    # 单一维护函数:只认 working 叶子、叶子优先、实时活动时撤除、否则原地改文本或补建。
    assert "export function syncPipelineThinking(stackRoot)" in app_source
    sync = app_source.split("export function syncPipelineThinking(stackRoot)", 1)[1].split("\n}\n", 1)[0]
    assert "stackRoot.querySelectorAll('[data-step-status=\"working\"]')" in sync
    assert "body.contains(child)" in sync  # 叶子优先:父步骤让位给更深的 working 子步骤
    assert "stepBodyHasLiveActivity(body)" in sync  # 有实时活动 → 撤除占位
    assert 'body.querySelector(":scope > .pipeline-thinking")' in sync  # 只认本 body 直属占位
    assert "body.append(buildPipelineThinkingIndicator(elapsed))" in sync  # 无占位则按秒数补建
    assert "label.textContent = pipelineThinkingLabel(elapsed);" in sync  # 已有则原地改文本保住流光相位
    # 实时活动判据覆盖进行中工具/工具组、真实思考块与流式助手消息。
    live = app_source.split("function stepBodyHasLiveActivity(body)", 1)[1].split("\n}\n", 1)[0]
    for sel in (".tool-card.is-active", ".tool-group.is-active", ".message-agent.is-streaming"):
        assert sel in live
    # 核心 bug 修复:流水线段消息的 .is-streaming 会挂到整步结束,只有最近 delta 抵达才算「正在流式」,
    # 否则视为停顿放行占位——否则事件间隙占位被这枚陈旧标记永久压制。
    assert "Date.now() - lastStreamDeltaAt < PIPELINE_STREAM_SILENCE_MS" in live
    assert "const PIPELINE_STREAM_SILENCE_MS = 1500;" in app_source
    # 流式助手消息打 is-streaming,供上面的判据识别。
    assert 'article.classList.add("is-streaming");' in app_source
    # handleStreamEvent 收到 text/thinking delta 时打点 lastStreamDeltaAt,喂给上面的时效判据。
    assert 'if (event.type === "assistant.text.delta" || event.type === "assistant.thinking.delta") {' in app_source
    assert "lastStreamDeltaAt = Date.now();" in app_source
    # renderMessages 末尾及心跳按模式分派:流水线→逐叶子;普通→底部单枚。分派内仍调 syncPipelineThinking。
    assert "syncPipelineThinking(stack);" in app_source
    assert "stack.append(buildPipelineThinkingIndicator());" not in app_source
    assert "appendPipelineStepThinking" not in app_source  # 旧的整栈注入函数彻底退场


def test_pipeline_compaction_bar_hosted_in_working_step() -> None:
    # 用户反馈:流水线自动压缩时压缩条「跑到最底下」,应渲染在触发压缩的进行中 step 内。
    # syncPipelineThinking 在压缩进行中把 buildCompactionIndicator 挂进宿主步骤体(compactionHost),
    # 撤掉该步骤的流光占位;renderMessages 尾部的栈底追加须按流水线模式跳过,避免与步骤内那枚重复。
    app_source = _source(APP_JS)
    sync = app_source.split("export function syncPipelineThinking(stackRoot)", 1)[1].split("\n}\n", 1)[0]
    assert 'const compacting = state.compaction?.status === "running";' in sync
    # 压缩条挂进宿主步骤体(而非撤除)。
    assert "compactionHost.append(buildCompactionIndicator(state.compaction))" in sync
    # 无宿主步骤时的栈底兜底,避免压缩条彻底消失。
    assert "stackRoot.append(buildCompactionIndicator(state.compaction))" in sync
    # renderMessages 尾部:流水线模式跳过栈底追加(交由 syncPipelineThinking 全权负责)。
    assert (
        "if (!(isPipelineTranscript(state) && !lastRenderPostHandoffNormal)) {\n"
        "      stack.append(buildCompactionIndicator(state.compaction));" in app_source
    )
    # 嵌入步骤体的压缩条去掉「转录栈底部」用的顶部分隔线/宽度约束。
    styles = _source(STYLES_CSS)
    assert ".pipeline-step-body > .context-compaction {" in styles
    step_bar_block = styles.split(".pipeline-step-body > .context-compaction {", 1)[1].split("}", 1)[0]
    assert "border-top: none" in step_bar_block


def test_pipeline_compaction_bar_hosted_by_group_id() -> None:
    # 并行候选阶段(两个方案同时跑 模板生成)下,「首个进行中叶子」启发式会把方案2的压缩条错挂到
    # 方案1。修复:compaction.started SSE 带 groupId,步骤体打 data-group-id,syncPipelineThinking
    # 优先按 groupId 精确匹配宿主步骤,缺失/未匹配才退回首个进行中叶子。
    app_source = _source(APP_JS)
    sync = app_source.split("export function syncPipelineThinking(stackRoot)", 1)[1].split("\n}\n", 1)[0]
    # 优先按 state.compaction.groupId 匹配进行中步骤体的 data-group-id。
    assert "String(state.compaction.groupId" in sync
    assert "body.dataset.groupId === compactionGroupId" in sync
    # 未匹配 groupId 时退回首个进行中叶子。
    assert "compactionHost = leafBodies[0]" in sync
    # 步骤体在 renderPipelineMarkerGroup 里打上 groupId。
    assert 'body.dataset.groupId = String(message.pipelineStep?.groupId || "")' in app_source


def test_pipeline_thinking_label_rotates_words_with_elapsed_seconds() -> None:
    # 文案每 3 秒在【处理中/执行中/进行中/运行中】间轮换,附已等待整秒数(类似 Claude Code)。
    app_source = _source(APP_JS)
    assert "export function pipelineThinkingLabel(elapsedMs = 0)" in app_source
    assert (
        'export const PIPELINE_THINKING_WORDS = [t("Processing"), t("Executing"), t("In progress"), t("Running")];'
        in app_source
    )
    label = app_source.split("export function pipelineThinkingLabel(elapsedMs = 0)", 1)[1].split("\n}\n", 1)[0]
    # 负值归零,3 秒换一词(floor(s/3)%4),文案拼上整秒数。
    assert "Math.max(0, Math.floor(elapsedMs / 1000))" in label
    assert "Math.floor(totalSeconds / 3) % PIPELINE_THINKING_WORDS.length" in label
    assert "${word}… ${totalSeconds}s" in label


def test_pipeline_thinking_heartbeat_wired_to_render() -> None:
    # 核心 bug 修复:静默期无 SSE 事件也由 1s 心跳定时器每秒 syncTurnThinking 补建占位并累加秒数;
    # render 末尾对称启停,门槛放宽为「回合活跃」(普通与流水线同门),内部按模式分派。
    app_source = _source(APP_JS)
    assert "const PIPELINE_THINKING_TICK_MS = 1000;" in app_source
    assert "const pipelineThinkingSince = new Map();" in app_source
    assert "setTimeout(pipelineThinkingTick, PIPELINE_THINKING_TICK_MS)" in app_source
    # render(state) 末尾接线启停。
    assert "syncPipelineThinkingHeartbeat(state);" in app_source
    # 启停门槛放宽:任一模式回合活跃即启动心跳(普通模式也需静默补建)。
    assert "const active = state.currentTurnActive === true;" in app_source
    # tick 与 renderMessages 尾都经 syncTurnThinking 按 isPipelineTranscript 分派。
    dispatch = app_source.split("function syncTurnThinking(stack)", 1)[1].split("\n}\n", 1)[0]
    assert "isPipelineTranscript(state)" in dispatch
    assert "syncPipelineThinking(stack);" in dispatch
    assert "syncNormalThinking(stack);" in dispatch
    # 停机时清空计时键/标量,防跨会话泄漏。
    assert "pipelineThinkingSince.clear();" in app_source
    assert "normalThinkingSince = 0;" in app_source


def test_normal_mode_thinking_indicator_wired() -> None:
    # 普通(非流水线)模式也在活跃回合的静默间隙于 message-stack 底部挂单枚「处理中… Ns」占位,
    # 复用流水线的 label/indicator;有实时活动(内联/独立工具、流式、真实思考)或 lastError/压缩时不补。
    app_source = _source(APP_JS)
    assert "let normalThinkingSince = 0;" in app_source
    assert "export function syncNormalThinking(stackRoot)" in app_source
    normal = app_source.split("export function syncNormalThinking(stackRoot)", 1)[1].split("\n}\n", 1)[0]
    # lastError / 压缩进行中 / 有实时活动时撤占位并归零计时。
    assert "state.lastError?.message" in normal
    assert 'state.compaction?.status === "running"' in normal
    assert "normalTurnHasLiveActivity(stackRoot)" in normal
    assert "normalThinkingSince = 0;" in normal
    # 否则起算/续算并补建到 stack 末尾或原地改文本。
    assert "normalThinkingSince = Date.now();" in normal
    assert "stackRoot.append(buildPipelineThinkingIndicator(elapsed));" in normal
    assert "label.textContent = pipelineThinkingLabel(elapsed);" in normal
    assert 'stackRoot.querySelector(":scope > .pipeline-thinking")' in normal
    # 实时活动判据:正文区(复用 stepBodyHasLiveActivity)或独立工具活动区的进行中工具。
    live = app_source.split("function normalTurnHasLiveActivity(stackRoot)", 1)[1].split("\n}\n", 1)[0]
    assert "stepBodyHasLiveActivity(stackRoot)" in live
    assert 'byShell("tool-activity-stack")' in live
    assert ".tool-card.is-active, .tool-group.is-active" in live


def test_thread_header_derives_title_from_messages_when_backend_title_empty(tmp_path) -> None:
    # 新会话后端 title 恒为 "(empty)",header 不能一直显示 "(empty)";应像侧边栏一样从
    # 最后一条(退而首条)用户消息派生标题。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = { getElementById() { return null; } };
            const { resolveThreadTitle, deriveThreadTitleFromMessages } = await import(__APP_MODULE__);

            const emptyBackend = resolveThreadTitle({
              currentSession: { title: "(empty)", sessionId: "sess-1" },
              messages: {
                a: { role: "user", text: "第一条 prompt", stored: true, sequence: 1 },
                b: { role: "assistant", text: "回复", stored: true, sequence: 2 },
                c: { role: "user", text: "最新一条 prompt", stored: true, sequence: 3 },
              },
            });

            // 手动命名过的会话:后端标题优先,不被消息覆盖。
            const named = resolveThreadTitle({
              currentSession: { title: "我的命名", sessionId: "sess-1" },
              messages: { c: { role: "user", text: "最新一条 prompt", stored: true, sequence: 3 } },
            });

            // 没有任何用户消息时回退到 currentThreadTitle(sessionId)。
            const noMessages = resolveThreadTitle({
              currentSession: { title: "", sessionId: "sess-9" },
              messages: {},
            });

            const multiline = deriveThreadTitleFromMessages({
              messages: { a: { role: "user", text: "行一\\n行二", stored: true, sequence: 1 } },
            });

            console.log(JSON.stringify({ emptyBackend, named, noMessages, multiline }));
            """
        ),
    )

    assert output["emptyBackend"] == "最新一条 prompt"
    assert output["named"] == "我的命名"
    assert output["noMessages"] == "sess-9"
    assert output["multiline"] == "行一 行二"


def test_transcript_message_stack_aligns_content_to_top() -> None:
    # 短会话此前用 align-content: end 被钉在底部,看起来像“从下面开始”;改为从顶部排布。
    styles = _source(STYLES_CSS)
    block = styles.split(".transcript-panel .message-stack {", 1)[1].split("}", 1)[0]
    assert "align-content: start" in block
    assert "align-content: end" not in block


def test_api_list_sessions_supports_project_cwd_filter(tmp_path) -> None:
    output = _run_api_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.window = { location: { origin: "http://localhost" } };
            const calls = [];
            globalThis.fetch = async (url, options) => {
              calls.push({ url, options });
              return {
                ok: true,
                headers: { get: () => "application/json" },
                json: async () => ({ sessions: [] }),
              };
            };

            const { listSessions } = await import(__API_MODULE__);
            await listSessions({ limit: 7, cwd: "/tmp/a workspace", projectLimit: 11, perProjectLimit: 5 });

            console.log(JSON.stringify({ url: calls[0].url, cache: calls[0].options.cache }));
            """
        ),
    )

    assert output["url"] == "/api/sessions?limit=7&cwd=%2Ftmp%2Fa+workspace&projectLimit=11&perProjectLimit=5"
    assert output["cache"] == "no-store"


def test_api_update_session_sends_patch_title(tmp_path) -> None:
    output = _run_api_script(
        tmp_path,
        textwrap.dedent(
            """
            const calls = [];
            globalThis.fetch = async (url, options) => {
              calls.push({ url, options });
              return {
                ok: true,
                headers: { get: () => "application/json" },
                json: async () => ({ title: "新标题" }),
              };
            };

            const { updateSession } = await import(__API_MODULE__);
            await updateSession("session-1", { title: "新标题" });

            console.log(JSON.stringify({
              url: calls[0].url,
              method: calls[0].options.method,
              body: JSON.parse(calls[0].options.body),
              cache: calls[0].options.cache,
            }));
            """
        ),
    )

    assert output == {
        "url": "/api/sessions/session-1",
        "method": "PATCH",
        "body": {"title": "新标题"},
        "cache": "no-store",
    }


def test_session_load_preserves_project_groups() -> None:
    source = _source(APP_JS)

    assert "projectGroups: state.projectGroups || []" in source


def test_app_uses_versioned_api_module_import() -> None:
    source = _source(APP_JS)

    assert 'from "./api.js?v=' in source


def test_app_disambiguates_duplicate_project_basenames(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { projectDisplayLabels } = await import(__APP_MODULE__);
            const labels = projectDisplayLabels([
              "/tmp/e2e/image-initial/workspace",
              "/tmp/e2e/image-normal-handoff/workspace",
              "/tmp/root-a/shared/workspace",
              "/tmp/root-b/shared/workspace",
              "/Users/me/open_repo/iac-code3/.worktrees/feature-web",
            ]);

            console.log(JSON.stringify(labels));
            """
        ),
    )

    assert output["/tmp/e2e/image-initial/workspace"] == "image-initial/workspace"
    assert output["/tmp/e2e/image-normal-handoff/workspace"] == "image-normal-handoff/workspace"
    assert output["/tmp/root-a/shared/workspace"] == "root-a/shared/workspace"
    assert output["/tmp/root-b/shared/workspace"] == "root-b/shared/workspace"
    assert output["/Users/me/open_repo/iac-code3/.worktrees/feature-web"] == "feature-web"


def test_app_project_labels_keep_searching_when_deeper_parent_can_disambiguate(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { projectDisplayLabels } = await import(__APP_MODULE__);
            const labels = projectDisplayLabels([
              "/tmp/root-a/shared/workspace",
              "/tmp/root-b/shared/workspace",
            ]);

            console.log(JSON.stringify(labels));
            """
        ),
    )

    assert output["/tmp/root-a/shared/workspace"] == "root-a/shared/workspace"
    assert output["/tmp/root-b/shared/workspace"] == "root-b/shared/workspace"


def test_app_project_labels_treat_timestamp_run_directories_as_normal_path_parts(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { projectDisplayLabels } = await import(__APP_MODULE__);
            const labels = projectDisplayLabels([
              "/tmp/iac-code-repl-e2e-runs/ask-waiting/20260620T115427Z-36937-e69d6cf4/workspace",
              "/tmp/iac-code-repl-e2e-runs/ask-waiting/20260619T151857Z-52951-b639bedf/workspace",
              "/tmp/iac-code-repl-e2e-runs/rollback-step4-selection/20260620T121845Z-36937-2301e039/workspace",
            ]);

            console.log(JSON.stringify(Object.values(labels)));
            """
        ),
    )

    assert output == [
        "20260620T115427Z-36937-e69d6cf4/workspace",
        "20260619T151857Z-52951-b639bedf/workspace",
        "20260620T121845Z-36937-2301e039/workspace",
    ]


def test_app_preserves_backend_project_labels(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { applyProjectDisplayLabels } = await import(__APP_MODULE__);
            const groups = applyProjectDisplayLabels([
              { key: "-Users-ehzyo-repo-empty-project", label: "empty-project", sessions: [], total: 0 },
            ]);

            console.log(JSON.stringify(groups[0]));
            """
        ),
    )

    assert output["label"] == "empty-project"


def test_app_relative_time_uses_codex_week_scale(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };
            Date.now = () => new Date("2026-06-27T12:00:00Z").getTime();

            const { relativeTimeLabel } = await import(__APP_MODULE__);
            const labels = [
              relativeTimeLabel({ updatedAt: "2026-06-27T11:57:00Z" }),
              relativeTimeLabel({ updatedAt: "2026-06-27T07:00:00Z" }),
              relativeTimeLabel({ updatedAt: "2026-06-26T12:00:00Z" }),
              relativeTimeLabel({ updatedAt: "2026-06-20T12:00:00Z" }),
              relativeTimeLabel({ updatedAt: "2026-06-13T12:00:00Z" }),
            ];

            console.log(JSON.stringify(labels));
            """
        ),
    )

    assert output == ["3m", "5h", "1d", "1w", "2w"]


def test_sidebar_threads_use_mode_icons_and_pin_archive_actions() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    assert 'data-app-shell="pinned-session-list"' in html
    assert html.index('data-app-shell="pinned-session-list"') < html.index('data-app-shell="session-list"')

    for snippet in [
        "pinnedSessions",
        'byShell("pinned-session-list")',
        "thread-mode-icon",
        "thread-action-pin",
        "thread-action-archive",
        "toggleSessionPinned",
        "archiveSession",
        "setThreadActionTooltip",
        "thread-action-tooltip",
        "data-tooltip",
        "is-tooltip-open",
        "is-action-hovered",
        'button.closest(".thread-item")',
        "mouseenter",
        "mouseleave",
        't("Pin conversation")',
        't("Unpin conversation")',
        't("Archive conversation")',
    ]:
        assert snippet in app_source

    assert 'metaText === "pipeline"' not in app_source
    assert "thread-status-badge" not in app_source

    for snippet in [
        ".pinned-thread-nav",
        ".pinned-thread-nav::before",
        # i18n:置顶标签经 data-label(applyDomI18n 按 UI 语言写入)渲染,CSS 不再硬编码中文。
        "content: attr(data-label)",
        ".thread-mode-icon.is-pipeline-mode",
        ".session-item .thread-mode-icon",
        ".session-item .thread-mode-icon {\n  color: var(--codex-muted)",
        "--thread-pin-icon: url(",
        "--thread-unpin-icon: url(",
        "--thread-archive-icon: url(",
        "-webkit-mask: var(--thread-action-icon) center / contain no-repeat",
        ".thread-action-tooltip",
        ".thread-action:hover .thread-action-tooltip",
        ".thread-action.is-tooltip-open .thread-action-tooltip",
        ".thread-item.is-action-hovered .thread-actions",
        ".thread-actions",
        ".session-item .thread-actions",
        ".thread-action",
        "overflow: visible",
        "text-overflow: clip",
        ".thread-action-pin",
        ".thread-action-pin.is-pinned",
        ".thread-action-archive",
        ".project-name",
        "font-weight: 400",
        ".thread-item .thread-title",
        "font-weight: 400",
    ]:
        assert snippet in styles


def test_styles_use_codex_dark_visual_tokens() -> None:
    styles = _source(STYLES_CSS)

    for token in [
        "color-scheme: dark",
        "--codex-bg: #1a1a1a",
        "--codex-rail: #2c2d2e",
        "--codex-panel: #1a1a1a",
        "--codex-panel-raised: #2b2b2b",
        "--codex-hover: rgba(255, 255, 255, 0.065)",
        "--codex-active: #4b4b4c",
        "--codex-text: rgba(255, 255, 255, 0.92)",
        "--codex-muted: rgba(255, 255, 255, 0.56)",
        "--codex-border: rgba(255, 255, 255, 0.11)",
    ]:
        assert token in styles

    for old_green in ["#eef3f0", "#1f6f64", "#144d46", "#e2f1ed", "#32734f", "#e7f4ec"]:
        assert old_green not in styles


def test_styles_make_workbench_look_like_codex_tooling() -> None:
    styles = _source(STYLES_CSS)
    html = _source(INDEX_HTML)

    for snippet in [
        "grid-template-columns: minmax(236px, 292px) minmax(0, 1fr)",
        "background: var(--codex-bg)",
        ".brand-mark {\n  display: none",
        "box-shadow: none",
        ".workspace-panel {\n  display: none",
        ".workspace-panel > section {\n  display: none",
        ".workspace-panel.has-tools > section:nth-of-type(2)",
        ".session-item.is-active {\n  background: var(--codex-active)",
        ".session-item.is-active::before",
        "background: var(--codex-text)",
        ".sidebar-footer",
        ".sidebar-settings-action",
        ".message-empty {\n  justify-self: center",
        ".composer-input-wrap {\n  position: relative",
        "border-radius: 22px",
        ".send-action {\n  order: 10",
        "width: 1.92rem",
        "background: var(--codex-text)",
        ".workspace-dialog {\n  grid-template-columns: minmax(236px, 292px) minmax(0, 1fr)",
        '.workspace-dialog:has([data-workspace-panel="settings"]:not([hidden]))',
        "width: 100vw",
        ".workspace-tabs {\n  grid-column: 1",
        ".workspace-tabs button.is-active {\n  background: var(--codex-active)",
        '.workspace-tab-panel[data-workspace-panel="settings"] {\n  max-width: 720px',
        "width: 720px",
        "justify-items: center",
        "--codex-sidebar-icon-new-thread",
        "--codex-sidebar-icon-search",
        "--codex-sidebar-icon-skills",
        "--codex-sidebar-icon-settings",
        "-webkit-mask: var(--sidebar-icon) center / contain no-repeat",
        ".project-menu::before",
        ".project-new-thread::after",
        '.workspace-tab-panel[data-workspace-panel="search"] .workspace-input',
        ".workspace-field > span {\n  color: var(--codex-muted)",
        ".workspace-field-desc {\n  color: var(--codex-muted)",
        ".workspace-cloud-layout {",
        ".workspace-field-hint {",
        ".workspace-cloud-mode-fields > .workspace-field",
        ".workspace-settings-group-title {\n  margin: 0",
        ".transcript-panel .suggestion-item {\n  display: grid",
        ".command-palette-dialog",
        ".queued-input-list",
        ".queued-input-row",
        ".queued-input-steer",
        ".queued-input-remove",
        ".queued-input-more",
        ".pipeline-step.is-active {\n  border-color: var(--codex-border-strong)",
        ".pipeline-candidate.is-selected {\n  border-color: var(--codex-border-strong)",
        ".pipeline-notice {\n  border-color: var(--codex-border-strong)",
    ]:
        assert snippet in styles

    for snippet in [
        "sidebar-action-icon-new",
        "sidebar-action-icon-search",
        "sidebar-action-icon-skills",
        "sidebar-action-icon-settings",
    ]:
        assert snippet in html


def test_sidebar_uses_codex_project_thread_navigation() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    for hook in [
        'data-app-shell="sidebar-global-actions"',
        'data-app-shell="session-list"',
        'data-app-shell="sidebar-search"',
        'data-app-shell="sidebar-skills"',
        'data-app-shell="workspace-open-config"',
    ]:
        assert hook in html

    assert 'class="sidebar-footer"' in html
    assert '<span data-i18n="New chat">New chat</span>' in html
    assert '<span data-i18n="Search">Search</span>' in html
    assert '<span data-i18n="Plugins">Plugins</span>' in html
    assert '<span data-i18n="Settings">Settings</span>' in html
    assert "New thread" not in html
    assert "New Session" not in html
    assert 'label: t("New chat")' in app_source
    assert 'label: t("Plugins")' in app_source
    # Cmd+N 监听已移除(与浏览器自身快捷键冲突);Cmd+K 仍打开命令面板。
    assert 'event.key.toLowerCase() === "k"' in app_source
    assert "(event.metaKey || event.ctrlKey)" in app_source
    assert "startNewSessionDraft" in app_source
    assert "createSessionForSubmit" in app_source
    assert "promoteMaterializedDraftSession" in app_source
    assert "void startNewSessionDraft()" in app_source
    assert "void startNewSessionDraft({ cwd: group.key })" in app_source
    assert "const session = await api.createSession(options);" not in app_source
    assert 'data-app-shell="command-palette"' in html
    assert 'data-app-shell="command-palette-search"' in html
    assert 'data-app-shell="command-palette-list"' in html
    assert 'data-app-shell="queued-inputs"' in html
    for hook in [
        'data-app-shell="draft-session-controls"',
        'data-app-shell="draft-project-control"',
        'data-app-shell="draft-project-menu"',
        'data-app-shell="draft-project-new-menu"',
        'data-app-shell="draft-mode-control"',
        'data-app-shell="draft-mode-menu"',
        'data-app-shell="draft-pipeline-menu"',
        "draft-mode-menu",
        "draft-pipeline-submenu",
    ]:
        assert hook in html

    for snippet in [
        "groupSessionsByProject",
        "project-group",
        "project-row",
        "project-collapse",
        "project-actions",
        "project-menu",
        "project-new-thread",
        "project-show-more",
        "thread-list",
        "thread-item",
        "thread-title",
        "thread-meta",
        "toggleProjectCollapsed",
        "expandedProjectKeys",
        "loadingProjectKeys",
        "renderProjectThreadNavigation",
        "PROJECT_THREAD_PREVIEW_LIMIT",
        "PROJECT_THREAD_EXPANDED_LIMIT",
        "projectDisplayLabels",
        "newSessionDraft",
        "newSessionCreatePayload",
        "renderDraftSessionControls",
        "DEFAULT_PIPELINE_NAME",
        't("Use directory")',
        "renderDraftProjectNewMenu",
        "renderDraftPipelineSubmenu",
        't("New project")',
        't("New blank project")',
        't("Use existing folder")',
        't("Sales pipeline")',
        't("Pipeline planning, generation, and validation for sales scenarios")',
        't("Pipeline mode")',
        'iconClass: "is-folder"',
        'iconClass: "is-selling-pipeline"',
        'draft.mode === "pipeline" ? "is-selling-pipeline" : "is-normal-mode"',
        'active: draft.mode === "pipeline" && option.id === draft.pipelineName',
        "menu.scrollTop = 0",
    ]:
        assert snippet in app_source

    assert "Pipeline 模式" not in app_source
    assert "detail: key," not in app_source
    # 空状态文案改为 IaC Code 相关中文,不再出现英文占位。
    assert "Start building your infrastructure" in app_source
    assert "Describe a task, command, or infrastructure change and hand it to IaC Code." in app_source
    assert "Let's build" not in app_source
    assert "Start with a task, command, or infrastructure change." not in app_source
    # 顶部右侧 Running/Idle 状态徽标已移除。
    assert 'data-app-shell="status"' not in html
    assert '<span class="status-pill"' not in html
    assert 'data-app-shell="draft-pipeline-control"' not in html
    assert "if (sessions.length === 0)" not in app_source
    assert '${session.mode || "normal"} / ${session.status || "idle"}' not in app_source
    assert "projectLimit: 50" not in app_source
    assert "暂无对话" not in app_source

    for snippet in [
        ".sidebar-global-actions",
        ".sidebar-action",
        ".project-group",
        ".project-row",
        ".project-collapse",
        ".project-actions",
        ".project-menu",
        ".project-new-thread",
        ".project-show-more",
        ".thread-list",
        ".thread-item",
        ".thread-title",
        ".thread-meta",
        ".thread-mode-icon",
        ".thread-actions",
        ".project-row-icon",
        "-webkit-mask: var(--codex-project-open-icon)",
        ".sidebar-action-icon {\n  position: relative",
        "border: 0",
        "font-size: 0.94rem",
        ".project-nav-header",
        ".project-nav-title",
        "font-size: 0.82rem",
        "font-weight: 420",
        ".project-name {\n  display: flex",
        "font-weight: 400",
        ".thread-item.is-active .thread-title {\n  color: var(--codex-text);\n  font-weight: 400",
        "--codex-sidebar-icon-new-thread",
        "--codex-sidebar-icon-search",
        "--codex-sidebar-icon-skills",
        "--codex-sidebar-icon-settings",
        "-webkit-mask: var(--sidebar-icon) center / contain no-repeat",
        "mask: var(--sidebar-icon) center / contain no-repeat",
        ".draft-session-controls",
        ".draft-session-control",
        ".draft-session-menu",
        ".draft-session-menu-item",
        ".draft-session-menu-search",
        ".draft-session-menu-search-wrap",
        ".draft-session-submenu",
        ".draft-mode-menu",
        "width: min(23rem",
        ".draft-pipeline-submenu",
        "left: calc(min(23rem",
        "width: min(18rem",
        ".draft-session-menu-icon.is-folder::before",
        ".draft-session-control-icon.is-normal-mode::after",
        ".draft-session-control-icon.is-pipeline-mode::after",
        "--pipeline-mode-icon: url(",
        ".draft-session-menu-icon.is-selling-pipeline::before",
        ".draft-session-control-icon.is-selling-pipeline::before",
        "--alicloud-logo-icon: url(",
        "viewBox='0 0 48 24'",
        ".draft-session-menu-icon.is-selling-pipeline {\n  color: #ff6a00;",
        "-webkit-mask:",
        "data:image/svg+xml",
        ".draft-session-menu-item.has-submenu",
        "color: var(--codex-text)",
        ".draft-session-control {\n  display: inline-flex",
        "font-size: 0.82rem",
        ".draft-session-menu-label {\n  overflow: hidden;\n  font-size: 0.84rem",
        "font-weight: 400;\n  line-height: 1.18",
        ".draft-session-menu-detail {\n  overflow: hidden;\n  color: var(--codex-muted);\n  font-size: 0.68rem",
    ]:
        assert snippet in styles

    assert ".thread-empty" not in styles
    assert "transform: rotate(-10deg)" not in styles
    assert "M3 8.4C3 5.9 4.9 4 7.4 4h9" not in styles

    # 项目选择弹层向上展开(bottom:100%)贴触发器上方;CSS 里的 max-height 只作 JS 未跑时的兜底,
    # 真正封顶由 app.js 的 clampDraftMenuToViewport 按触发器 rect 计算,故 CSS 仍保留合理上限 + 可滚动。
    # 三段式:菜单自身 flex 列 + overflow:hidden 不滚,只有中间 .draft-session-menu-list 滚动;
    # 搜索头与底栏 flex:none 固定。CSS max-height 仅作 JS 未跑时的兜底上限。
    draft_menu_block = _css_block(styles, ".draft-session-menu")
    assert "display: flex;" in draft_menu_block
    assert "flex-direction: column;" in draft_menu_block
    assert "max-height: min(25rem, calc(100vh - 6rem));" in draft_menu_block
    assert "overflow: hidden;" in draft_menu_block

    list_block = _css_block(styles, ".draft-session-menu-list")
    assert "flex: 1 1 auto;" in list_block
    assert "min-height: 0;" in list_block
    assert "overflow-y: auto;" in list_block
    footer_block = _css_block(styles, ".draft-session-menu-footer")
    assert "flex: none;" in footer_block

    # 关键:固定 rem 引用不到触发器到视口顶的真实距离(composer 多行/断点/窗口高度都会变),
    # 矮窗口里顶端仍越界。app.js 在打开各草稿菜单时按 picker 的 getBoundingClientRect 收紧 max-height,
    # 顶端恒不越界;四个 composer 弹层(项目/新建项目/模式/流水线)都要调用。
    app_source = _source(APP_JS)
    assert "function clampDraftMenuToViewport(menu)" in app_source
    assert 'menu.closest(".draft-session-picker")' in app_source
    assert "picker.getBoundingClientRect()" in app_source
    assert 'window.getComputedStyle(menu).bottom === "auto"' in app_source
    assert "menu.style.maxHeight = `min(25rem, ${Math.max(120, Math.round(available))}px)`;" in app_source
    assert app_source.count("clampDraftMenuToViewport(menu);") == 4
    # 项目菜单三段:搜索头直挂 menu,项目项进 .draft-session-menu-list,底栏进 .draft-session-menu-footer。
    assert 'list.className = "draft-session-menu-list";' in app_source
    assert 'footer.className = "draft-session-menu-footer";' in app_source


def test_mobile_sidebar_uses_drawer_instead_of_horizontal_session_strip() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)
    audit_source = _source(VISUAL_AUDIT_SCRIPT)

    assert 'data-app-shell="sidebar-drawer-toggle"' in html
    assert "toggleMobileSidebar" in app_source
    assert "setMobileSidebarOpen(false)" in app_source
    assert ".sidebar-drawer-toggle" in styles
    assert ".sidebar-footer {\n    position: fixed" in styles
    assert ".workbench.sidebar-open .sidebar-footer {\n    position: static" in styles
    assert "grid-template-rows: auto auto minmax(0, 1fr)" in styles
    assert ".workspace-settings-provider {\n    grid-template-columns: 1fr" in styles
    assert ".workbench.sidebar-open .session-rail" in styles
    assert ".workbench.sidebar-open .project-thread-nav" in styles
    assert "mobile-sidebar-open" in audit_source


def test_workspace_modal_uses_codex_tool_surfaces_instead_of_plain_forms() -> None:
    source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    for snippet in [
        "workspace-memory-notes",
        "workspace-memory-library",
        "workspace-skill-badge",
        "workspace-skill-name",
    ]:
        assert snippet in source

    for snippet in [
        ".workspace-memory-notes",
        ".workspace-memory-library",
        ".workspace-skill-badge",
        ".workspace-skill-name",
        ".workspace-search-surface",
        ".workspace-search-result-list",
        ".workspace-search-empty",
    ]:
        assert snippet in styles


def test_plugins_panel_skill_management_is_codex_style() -> None:
    source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    # 「插件」容器 + 「技能」区块(未来其它插件可追加同级区块)。
    assert 'textContent: t("Plugins")' in source
    assert 'textContent: t("Skills")' in source
    assert "workspace-plugins-panel" in source

    # 来源中文徽标 + token 估算(参照终端 skills_picker)。
    assert 'const SKILL_SOURCE_LABELS = { bundled: t("Built-in"), project: t("Project"), user: t("User") };' in source
    assert "function formatSkillTokens(contentLength)" in source
    assert "Math.ceil((Number(contentLength) || 0) / 4)" in source

    # 单一可搜索列表:本地即时过滤(name + description),不再有「加载技能」按钮。
    assert 'makeTextInput("workspace-skills-search"' in source
    assert 'searchInput.addEventListener("input", renderList)' in source
    assert "Load Skills" not in source
    assert "Save Disabled" not in source

    # 开关即时保存:切换后按全量 disabled 列表提交(带所选项目 cwd),失败回滚;不再堆 JSON 摘要。
    assert "workspace-switch" in source
    assert 'checkbox.addEventListener("change", () => saveToggle(skill, checkbox))' in source
    assert "api.saveDisabledSkills({" in source
    assert "disabled: disabledNames()," in source
    assert "cwd: selectedCwd," in source
    assert "skill.enabled = previous;" in source
    # 技能区改用轻量状态行,不再往 <pre> 堆 JSON 摘要。
    assert "workspace-skills-status" in source
    assert "showStatus" in source

    # 无活动会话时仍须展示内置技能:后端对空 sessionId 回落到默认项目 cwd,
    # 前端不再早退,竞态判定用容忍空会话的 isSkillsRequestStale(空 id === 空 id 视为一致)。
    assert 'showStatus("没有活动会话", true)' not in source
    assert "const isSkillsRequestStale = (requestedSessionId, token) =>" in source
    assert "context.sessionId() !== requestedSessionId || token !== requestToken" in source

    # 锁定(内置)技能开关禁用且置灰。
    assert "checkbox.disabled = locked;" in source
    assert 'toggle.classList.toggle("is-locked", true);' in source

    # 项目选择器:项目级技能按所选项目发现,与记忆面板共用 listProjects 后端。
    assert "workspace-skills-project-picker" in source
    assert 'textContent: t("Selected project")' in source
    assert "api.listProjects()" in source
    assert "await api.getSkills(requestedSessionId, selectedCwd)" in source
    assert 'projectSelect.addEventListener("change"' in source
    # 项目选择器为面板级控件,置于「插件」标题正下方(技能区块之前)。
    assert "panel.append(heading, projectPickerRow, skillsHead, card)" in source

    # 保存成功的「已更新。」提示是瞬时反馈,短暂后自动收起,不残留(无新请求接管时)。
    assert "if (settledToken === requestToken) {" in source
    assert "clearStatus();" in source

    # 面板间距:标题贴紧配置(head 下边距收紧),避免 tab-panel gap 叠加导致的过大留白。
    assert ".workspace-plugins-panel .workspace-settings-group-head" in styles

    for selector in [
        ".workspace-skill-badge",
        ".workspace-skill-source-bundled",
        ".workspace-skill-source-project",
        ".workspace-skill-source-user",
        ".workspace-switch.is-locked",
        ".workspace-skills-status.is-error",
        ".workspace-skills-project-row",
        ".workspace-skills-project-picker",
    ]:
        assert selector in styles


def test_plugins_panel_hosts_skills_and_mcp_subtabs() -> None:
    # MCP 不再是独立的顶层导航项,已并入「插件」面板,通过横向子标签(技能 / MCP)切换。
    html = _source(INDEX_HTML)
    source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    # 左侧导航移除独立的 MCP 项(占位标记与图标一并清理)。
    assert 'data-workspace-tab="mcp"' not in html
    assert "workspace-tab-icon-mcp" not in html
    assert 'data-workspace-panel="mcp"' not in html
    # NAV_GROUPS 不再含独立 MCP 项(改用逐字段核对,避免与 PLUGINS_SUBTABS 里同样的
    # `{ id: "mcp", label: "MCP" }` 字面量冲突)。skills 之后是 devOnly 的 developer 项。
    assert ('{ id: "memory", label: t("Memory") },\n      { id: "skills", label: t("Plugins") },\n') in source
    assert '{ id: "mcp", label: "MCP" },\n    ],' not in source

    # 「插件」容器包住两个子面板,并注册为唯一的 skills 面板控制器。
    assert "function createPluginsPanel(api, context)" in source
    assert 'panelControllers.set("skills", createPluginsPanel(api, context))' in source
    assert 'panelControllers.set("mcp"' not in source
    # 容器内部复用原有的两个面板工厂。
    assert "skills: createSkillsPanel(api, context)" in source
    assert "mcp: createMcpPanel(api, context)" in source
    # 剥掉子面板自带 <h3> 与 data-workspace-panel 标记,避免与容器标题/子标签重复。
    assert 'child.panel.querySelector(":scope > h3")?.remove?.()' in source
    assert 'child.panel.removeAttribute?.("data-workspace-panel")' in source

    # 横向子标签栏:技能 / MCP,默认选中技能;切换时激活对应子面板。
    assert "const PLUGINS_SUBTABS = [" in source
    assert '{ id: "skills", label: t("Skills") }' in source
    assert '{ id: "mcp", label: "MCP" }' in source
    assert "workspace-plugins-subtabs" in source
    assert "workspace-plugins-subtab" in source
    assert "pluginsSubtab: tab.id" in source
    assert 'showSub("skills")' in source
    assert "children[activeSub].activate?.()" in source

    # 子标签栏用文件夹式激活:上凸圆角描边 + 底边用 --codex-bg 顶开底部横线,
    # 仅用中性 token,象牙等主题下不会黑底黑字/白底白字。
    assert ".workspace-plugins-subtabs" in styles
    assert ".workspace-plugins-subtab.is-active" in styles
    assert "border-radius: 9px 9px 0 0;" in styles
    assert "border-color: var(--codex-border);" in styles
    assert "border-bottom-color: var(--codex-bg);" in styles


def test_memory_library_scopes_reset_confirm_and_fix_spacing() -> None:
    workspace_source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    # #1:记忆库分「全局/项目」作用域,条目带作用域徽标,并随顶部项目选择刷新。
    assert (
        'const MEMORY_SCOPE_LABELS = { global: t("Global memory"), project: t("Project memory") };' in workspace_source
    )
    assert "workspace-memory-scope workspace-memory-scope-${scope}" in workspace_source
    assert "MEMORY_SCOPE_LABELS[scope] || scope" in workspace_source
    assert "api.searchLegacyMemory(query, selectedCwd)" in workspace_source
    assert 'api.deleteLegacyMemory(memoryId, memoryScope === "project" ? selectedCwd : "", memoryScope)' in (
        workspace_source
    )
    assert ".workspace-memory-scope {" in styles
    assert ".workspace-memory-scope-project {" in styles

    # #2:「确认删除?」用面板级 armedDelete;点击别处复位,重建/reset 也复位。
    assert "let armedDelete = null;" in workspace_source
    assert "if (armedDelete?.button !== deleteButton) {" in workspace_source
    assert 'document.addEventListener("click", (event) => {' in workspace_source
    assert "if (armedDelete && !armedDelete.button.contains?.(event.target)) {" in workspace_source

    # #3:记忆面板专属间距——标题贴紧卡片、章节之间拉开更大的间距,避免各分区挤在一起。
    assert ".workspace-memory-panel {\n  gap: 0.4rem;\n}" in styles
    assert ".workspace-memory-panel .workspace-settings-group-head {\n  margin: 0 0 0.12rem;\n}" in styles
    assert (
        ".workspace-memory-panel .workspace-settings-group + .workspace-settings-group-head {\n"
        "  margin-top: 2.25rem;\n}"
    ) in styles


def test_general_panel_sections_have_chapter_spacing() -> None:
    # 常规面板此前没有分区间距规则,各分区(外来会话可见性/售卖流水线/新会话默认/配色方案)
    # 挤在一起。补齐与记忆/云面板同构的间距:标题贴紧自身卡片,与上一张卡片之间拉开章节间距。
    styles = _source(STYLES_CSS)

    assert ".workspace-other-panel {\n  gap: 0.4rem;\n}" in styles
    assert ".workspace-other-panel .workspace-settings-group-head {\n  margin: 0 0 0.12rem;\n}" in styles
    # 面板标题→首个分区标题、卡片→下一分区标题、裸 languageField→重启标题,统一 2.25rem 章节间距。
    assert (
        ".workspace-other-panel > h3 + .workspace-settings-group-head,\n"
        ".workspace-other-panel .workspace-settings-group + .workspace-settings-group-head,\n"
        ".workspace-other-panel .workspace-field + .workspace-settings-group-head {\n"
        "  margin-top: 2.25rem;\n}"
    ) in styles


def test_general_panel_section_order() -> None:
    # 章节顺序:新会话默认 → 配色方案(含界面语言)→ 售卖流水线 → 外来会话可见性 → 开发者模式。
    # 重启服务已从常规面板移至「开发」分页;languageField 紧随 themeGrid,保证 field→head
    # 相邻关系仍命中现有章节间距选择器。
    workspace_source = _source(WORKSPACE_JS)
    assert (
        "panel.append(\n"
        "    heading,\n"
        "    sessionDefaultsGroupHead,\n"
        "    sessionDefaultsCard,\n"
        "    themeGroupHead,\n"
        "    themeGrid,\n"
        "    languageField,\n"
        "    reviewStepGroupHead,\n"
        "    reviewStepCard,\n"
        "    groupHead,\n"
        "    card,\n"
        "    devModeGroupHead,\n"
        "    devModeCard,\n"
        "    status,\n"
        "  );"
    ) in workspace_source


def test_memory_project_picker_moves_to_panel_top_and_cloud_result_hides_json() -> None:
    workspace_source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    # #1:项目选择器提到「记忆」标题与「常驻记忆」之间的顶部选择器行,
    # 不再塞在「项目记忆」卡片头里。
    assert 'makeElement("div", { className: "workspace-memory-project-row" })' in workspace_source
    assert 'textContent: t("Selected project")' in workspace_source
    assert "panel.append(heading, projectPickerRow, notesHead, notesCard" in workspace_source
    # 「项目记忆」卡片不再把 projectSelect 作为 headExtra 传入。
    assert (
        'makeNote(t("Project memory"), t("Selected project"), projectArea, projectStatus, saveProjectButton)'
        in workspace_source
    )
    assert ".workspace-memory-project-row {" in styles
    assert ".workspace-memory-project-label {" in styles

    # #2:云凭证面板不再把凭证摘要 JSON 堆进 result;result 仅承载状态提示,成功后隐藏。
    assert "setOutput(result, { cloud: summaryPayload })" not in workspace_source
    assert "hideCloudResult();" in workspace_source
    assert 'showCloudStatus(t("Loading cloud credentials…"))' in workspace_source
    assert 'showCloudStatus(t("Saving cloud credentials…"))' in workspace_source
    assert "阿里云凭证摘要会显示在这里。" not in workspace_source


def test_workspace_modal_controls_do_not_stretch_into_large_blocks() -> None:
    styles = _source(STYLES_CSS)

    assert ".workspace-tab-panel {\n  align-content: start" in styles
    assert ".workspace-action-row {\n  display: flex;\n  align-items: center" in styles
    assert (
        ".workspace-list dd {\n"
        "  max-width: 65%;\n"
        "  margin: 0;\n"
        "  min-width: 0;\n"
        "  overflow: hidden;\n"
        "  text-overflow: ellipsis;\n"
        "  white-space: nowrap"
    ) in styles


@pytest.mark.parametrize("script_path", [SMOKE_SCRIPT, VISUAL_AUDIT_SCRIPT])
def test_e2e_scripts_use_cross_platform_module_and_temporary_paths(script_path: Path) -> None:
    source = _source(script_path)

    assert 'import { fileURLToPath } from "node:url";' in source
    assert "fileURLToPath(import.meta.url)" in source
    assert "new URL(import.meta.url).pathname" not in source
    assert '"/tmp/' not in source


def test_visual_audit_script_captures_full_codex_review_matrix() -> None:
    source = _source(VISUAL_AUDIT_SCRIPT)

    for screenshot_name in [
        "desktop-default",
        "desktop-multi-session",
        "desktop-project-row-hover",
        "desktop-project-collapsed",
        "desktop-session-list-scrolled",
        "desktop-session-selected-after-scroll",
        "desktop-new-thread-draft",
        "desktop-project-menu-open",
        "desktop-sidebar-search",
        "desktop-sidebar-skills",
        "desktop-settings-button-hover",
        "command-palette",
        "command-palette-filtered",
        "transcript-normal-tool",
        "transcript-long-content",
        "transcript-blocking",
        "queued-input-accepted",
        "queued-attachment-error",
        "transcript-error",
        "composer-focused",
        "composer-multiline-draft",
        "composer-image-attachment",
        "composer-running",
        "suggestions-open",
        "suggestions-keyboard-scroll",
        "file-suggestions",
        "skill-suggestions",
        "shell-suggestions",
        "suggestions-exact-command",
        "transcript-local-shell",
        "transcript-shell-failure",
        "settings-modal",
        "settings-provider-saved",
        "settings-cloud-expanded",
        "settings-cloud-saved",
        "memory-modal",
        "memory-save-states",
        "memory-legacy-results",
        "memory-legacy-deleted",
        "skills-modal",
        "skills-toggle-hover",
        "skills-disabled-saved",
        "status-modal",
        "session-search-results",
        "session-search-empty",
        "pipeline-modal",
        "pipeline-candidate-selected",
        "pipeline-rollback",
        "command-auth",
        "command-model",
        "command-effort",
        "command-memory",
        "command-skills",
        "command-prompt",
        "command-help",
        "command-debug",
        "mobile-default",
        "mobile-sidebar-open",
        "mobile-sidebar-scrolled",
        "mobile-settings-modal",
        "mobile-memory-modal",
        "mobile-blocking",
        "mobile-suggestions",
        "tablet-default",
        "tablet-settings-modal",
    ]:
        assert screenshot_name in source

    assert "docs/web-repl-codex-visual-audit.md" in source
    assert "docs/web-ui-audit/comprehensive-screenshot-issue-report.md" in source
    assert "manualIssueFindings" in source
    assert "NEEDS_REVIEW" in source
    assert "Review Pass 1 - Completeness" in source
    assert "Review Pass 2 - Correctness" in source
    assert "Review Pass 3 - Contact Sheet Visual Sweep" in source
    assert "UI-005" in source
    assert "visual error state" in source
    assert "trigger rollback failure for visual audit" in source
    assert "queued follow-up while the turn is active" in source
    assert "setViewport(page, entry.viewport)" in source
    assert "visualFindings" in source
    assert "P0" in source
    assert "P1" in source
    assert "P2" in source
    assert 'waitForWorkspaceLoaded(page, "memory"' in source
    assert 'waitForWorkspaceLoaded(page, "skills"' in source
    assert "createPersistedSession" in source
    assert "button.session-item" not in source
    assert 'document.querySelectorAll(".session-item").length >= expected' in source
    assert '["input", "select", "textarea"].includes(item.tag)' in source


def test_composer_enter_submits_exact_command_instead_of_accepting_same_suggestion(tmp_path) -> None:
    output = _run_composer_script(
        tmp_path,
        textwrap.dedent(
            """
            const { shouldAcceptSuggestionOnEnter } = await import(__COMPOSER_MODULE__);

            console.log(JSON.stringify({
              partial: shouldAcceptSuggestionOnEnter("/au", { value: "/auth", kind: "command" }),
              exact: shouldAcceptSuggestionOnEnter("/auth", { value: "/auth", kind: "command" }),
              exactWithSpace: shouldAcceptSuggestionOnEnter("/auth ", { value: "/auth", kind: "command" }),
              noSuggestion: shouldAcceptSuggestionOnEnter("/auth", null),
            }));
            """
        ),
    )

    assert output == {
        "partial": True,
        "exact": False,
        "exactWithSpace": False,
        "noSuggestion": False,
    }


def test_index_exposes_live_frontend_mount_points() -> None:
    source = _source(INDEX_HTML)

    assert "/static/styles.css?v=" in source
    assert "/static/js/app.js?v=" in source

    for hook in [
        'data-app-shell="session-list"',
        'data-app-shell="message-stack"',
        'data-app-shell="blocking-stack"',
        'data-app-shell="tool-stack"',
        'data-app-shell="composer-form"',
        'data-app-shell="suggestions"',
        'data-app-shell="attachment-chips"',
        'data-app-shell="permission-mode-control"',
        'data-app-shell="permission-mode-menu"',
        'data-app-shell="composer-model-control"',
        'data-app-shell="composer-model-menu"',
        'data-app-shell="pipeline-workspace"',
        'data-app-shell="workspace-tabs"',
        'data-app-shell="workspace-content"',
        'data-app-shell="thread-title"',
        'data-app-shell="thread-menu-toggle"',
        'data-app-shell="thread-menu"',
        'data-app-shell="thread-pin"',
        'data-app-shell="thread-rename"',
        'data-app-shell="thread-archive"',
        'data-app-shell="pipeline-workspace-open"',
        'data-app-shell="app-modal"',
        'data-app-shell="app-modal-input"',
    ]:
        assert hook in source


def test_pipeline_workspace_entry_removed_but_code_retained_with_audit_coverage() -> None:
    # 遗留的 pipeline 工作区模态入口已从产品中下线(用户从未开放该功能):
    #   1. 标题栏「View pipeline」按钮恒隐藏 —— pipelineWorkspaceEntryVisible 恒返回 false;
    #   2. 候选选择不再自动弹出工作区 —— maybeOpenPipelineSelectionWorkspace 不再被调用。
    # 但底层渲染代码(隐藏 tab、components/pipeline.js、按钮 markup 与点击处理)保留、可回滚,
    # 且视觉审计仍通过临时揭开隐藏按钮覆盖该工作区的回归。
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)
    audit_source = _source(VISUAL_AUDIT_SCRIPT)
    workspace_source = _source(WORKSPACE_JS)

    # 底层代码保留:按钮 markup / 点击处理 / 隐藏态 CSS / 渲染函数都还在。
    assert 'data-app-shell="pipeline-workspace-open"' in html
    assert 'aria-label="View pipeline"' in html
    assert (
        'byShell("pipeline-workspace-open")?.addEventListener("click", () => openWorkspaceModal("pipeline"))'
        in app_source
    )
    assert 'const pipelineWorkspaceOpen = byShell("pipeline-workspace-open")' in app_source
    assert "pipelineWorkspaceOpen.hidden = !pipelineWorkspaceEntryVisible(state)" in app_source
    assert "export function pipelineWorkspaceEntryVisible" in app_source
    assert ".pipeline-workspace-open[hidden]" in styles

    # 入口已关闭:可见性判定恒 false,自动弹出调用已移除。
    entry_fn = app_source.split("export function pipelineWorkspaceEntryVisible", 1)[1][:200]
    assert "return false;" in entry_fn
    assert '=== "pipeline"' not in entry_fn
    assert "maybeOpenPipelineSelectionWorkspace(state)" not in app_source

    assert 'name: "mobile-pipeline-workspace"' in audit_source
    assert 'name: "pipeline-session-entry"' in audit_source
    assert 'await setViewport(page, "tablet");' in audit_source
    assert "showProgrammaticWorkspacePanelForVisualFixture" not in audit_source
    # 审计临时揭开隐藏入口后仍复用其真实点击处理打开工作区。
    assert "async function openLegacyPipelineWorkspace(page)" in audit_source
    assert "page.locator('[data-app-shell=\"pipeline-workspace-open\"]').click()" in audit_source
    assert "Exact /status command with suggestion list open." in audit_source
    assert "Exact /auth command with suggestion list open." not in audit_source
    assert "Search files/quick-open/history/transcript/error" not in audit_source
    assert "all 68 screenshots" not in audit_source
    assert "all ${captured.length} captured screenshots" in audit_source
    assert 'textContent: t("Pipeline")' in workspace_source
    assert ".workspace-tab-group-title {" in styles
    assert "white-space: nowrap" in styles


def test_index_and_workspace_component_expose_workspace_tabs() -> None:
    html = _source(INDEX_HTML)
    source = _source(WORKSPACE_JS)

    # 设置导航精简后仅保留:配置(模型/云凭证/记忆/插件)+ 历史(已归档对话)。
    # index.html 与 workspace.js 均已切到英文 msgid(workspace.js 经 t() 包裹)。
    for label in [
        "Models",
        "Cloud credentials",
        "Memory",
        "Plugins",
        "Archived conversations",
    ]:
        assert label in html
        assert label in source

    # 导航按 Codex 质感分组:muted 分组标题 + 每项带 mask 图标。
    for title in ["Configuration", "History"]:
        assert title in html
        assert title in source
    assert ".workspace-tab-group-title" in _source(STYLES_CSS)
    assert ".workspace-tab-icon::before" in _source(STYLES_CSS)
    assert "-webkit-mask: var(--wtab-icon) center / contain no-repeat" in _source(STYLES_CSS)

    # 会话组(状态/流水线)与搜索标签已移出导航;搜索设置框亦移除。面板仍在
    # buildPanels 注册,可经侧栏/命令编程式打开(默认落在「常规」)。
    assert '<p class="workspace-tab-group-title">会话</p>' not in html
    assert 'data-workspace-tab="status"' not in html
    assert 'data-workspace-tab="pipeline"' not in html
    assert 'data-workspace-tab="search"' not in html
    assert 'data-app-shell="workspace-settings-search"' not in html
    # 进入配置默认选中「常规」(other):初值 + 未知标签回落。
    assert 'let activeTab = "other";' in source
    assert "panelControllers.has(tabId)" in source


def test_status_panel_exposes_copy_session_id_control() -> None:
    """状态面板须有一枚复制图标按钮,点击把当前会话 ID 写入剪贴板,并有成功/失败反馈。"""
    source = _source(WORKSPACE_JS)
    css = _source(STYLES_CSS)

    # 剪贴板辅助:Clipboard API 优先 + execCommand 回退。
    assert "async function copyTextToClipboard(value)" in source
    assert "navigator?.clipboard?.writeText" in source
    assert 'document.execCommand("copy")' in source

    # 复制按钮构造 + 中文无障碍标签 + 目标 ID 由 render() 更新。
    assert '"workspace-status-copy"' in source
    assert '"workspace-status-copy-icon"' in source
    assert 't("Copy session ID")' in source
    assert "copyTextToClipboard(statusSessionId)" in source
    assert '[t("Session"), sessionId, copyIdButton]' in source

    # 图标样式:mask 复制图标 + 复制成功切换对勾。
    assert ".workspace-status-copy {" in css
    assert ".workspace-status-copy-icon {" in css
    assert '.workspace-status-copy[data-copied="yes"] .workspace-status-copy-icon {' in css


def test_workspace_model_panel_has_provider_subnav() -> None:
    source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)
    assert "createModelPanel" in source
    assert "createCloudPanel" in source
    assert '"data-workspace-panel": "model"' in source
    assert '"data-workspace-panel": "cloud"' in source
    assert "workspace-provider-nav" in source
    assert "Set as current model" in source
    assert "Save configuration" in source
    # 清空配置:整条重置(删模型/密钥/端点),danger 样式,active provider 禁用。
    assert "Clear configuration" in source
    assert "workspace-model-clear" in source
    assert "clearProviderConfig" in source
    assert ".workspace-action-danger" in styles
    assert ".workspace-model-layout" in styles
    assert ".workspace-provider-nav" in styles
    # 三栏并列:组 | 该组内 provider | 配置卡片。第 1 栏是分组列表(镜像 REPL 的
    # provider_groups),点组刷新第 2 栏,点 provider 刷新第 3 栏,同屏无返回。
    assert "workspace-model-groups" in source
    assert "workspace-model-group-item" in source
    assert "renderGroups" in source
    assert "renderProviders" in source
    assert "selectGroup" in source
    assert ".workspace-model-groups" in styles
    assert ".workspace-model-group-item" in styles
    assert "grid-template-columns: minmax(120px, 150px) minmax(150px, 200px) minmax(0, 1fr)" in styles
    # 第三方只读条目处理。
    assert 'provider.kind === "partner"' in source
    assert "workspace-provider-partner-note" in source
    assert ".workspace-provider-partner-note" in styles
    # 顶部只读摘要块已移除(模型/推理强度/API 密钥与下方可编辑字段逐条重复);
    # 唯一非重复的「状态」收成标题旁的徽章(当前模型/已配置/未配置),active 态绿色。
    assert "workspace-provider-status-badge" in source
    assert "workspace-provider-title-row" in source
    assert "statusBadge.dataset.state" in source
    assert "Current model" in source
    assert ".workspace-provider-title-row" in styles
    assert '.workspace-provider-status-badge[data-state="active"]' in styles
    # 模型面板配置卡片不再声明只读摘要 dl(与下方可编辑字段重复);结果面板的
    # "workspace-list workspace-status-summary" 不受影响。
    assert 'makeElement("dl", { className: "workspace-list" })' not in source
    # 连接处凹角保留:选中的 provider(第 2 栏)与配置卡片(第 3 栏)相接的上下
    # armpit 用径向遮罩切出凹弧。
    assert ".workspace-provider-nav-item.is-selected::before" in styles
    assert ".workspace-provider-nav-item.is-selected::after" in styles
    assert "radial-gradient(circle at top left, transparent 0.55rem, #000 0.55rem)" in styles
    # 已移除 nav 内部滚动条,列表完整展开。
    assert "max-height: 60vh" not in styles
    # 只读伙伴条目靠 fields.hidden 隐藏可编辑表单;display:grid 会盖过 UA 的
    # [hidden],需 class+属性 规则(0-2-0)重新压过,否则字段仍会显示。
    assert ".workspace-settings-group[hidden]" in styles
    # 模型/推理强度:枚举型服务商用原生 <select> 全量下拉,候选为空时退回可手动输入的
    # <input list> + <datalist> 组合框。二者共用 .workspace-choice 字段槽按候选项切换显隐。
    assert "makeCombobox" in source
    assert "workspace-combobox" in source
    assert "<datalist" in source or 'makeElement("datalist"' in source
    assert "makeChoiceField" in source
    assert "workspace-choice" in source
    assert ".workspace-choice > [hidden]" in styles
    # 操作按钮下移贴卡片底部(footer + margin-top:auto)。
    assert "workspace-provider-form-footer" in source
    assert ".workspace-provider-form-footer" in styles
    assert "margin-top: auto" in styles
    # OAuth 模式整行隐藏底部按钮:footer 自身 display:flex 会盖掉 hidden 属性,
    # 必须有带 [hidden] 的复位规则(0-2-0),否则 .hidden=true 失效、按钮照旧显示。
    assert ".workspace-provider-form-footer[hidden]" in styles
    # 首项选中时消除凹角浮块:卡片左上角变方 + 隐藏顶部 armpit。
    assert "is-first-provider-selected" in source
    assert ".workspace-model-layout.is-first-provider-selected" in styles


def test_workspace_model_panel_has_collapsible_advanced_knobs() -> None:
    # 「最大输出 tokens」+「思考预算」两个高级旋钮:默认折叠,展开才可见。
    source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)
    api_source = _source(API_JS)

    # 折叠容器 + chevron + aria-expanded 展开范式(与侧栏分组一致)。
    assert "workspace-advanced" in source
    assert "workspace-advanced-toggle" in source
    assert "workspace-advanced-chevron" in source
    assert '"aria-expanded": "false"' in source
    assert "Advanced settings" in source
    # 默认折叠:body 初始 hidden=true;toggle 翻转 aria-expanded 与 body.hidden。
    assert "advancedBody.hidden = true" in source
    assert 'advancedToggle.setAttribute("aria-expanded"' in source

    # 两个字段标记 + 标签。最大输出 tokens 恒显示;思考预算按能力门控。
    assert "workspace-model-max-tokens" in source
    assert "workspace-model-thinking-budget" in source
    assert "makeNumberInput" in source
    assert "Max output tokens" in source
    assert "Thinking budget" in source
    # 能力门控:仅 supportsThinkingBudget 的模型显示思考预算字段,占位回填 defaultThinkingBudget。
    assert "supportsThinkingBudget" in source
    assert "thinkingBudgetField.hidden = !supportsBudget" in source
    assert "defaultThinkingBudget" in source
    # 两个字段的 placeholder 都显示留空回落的具体默认值。
    assert "defaultMaxCompletionTokens" in source
    assert 't("Leave blank to use model default ({value})", { value: defaultMax })' in source
    assert 't("Leave blank to use model default ({value})", { value: defaultBudget })' in source
    # 回填已保存值(可清空 → ?? "");两个旋钮按模型存储,故按选中模型回填而非 provider 级。
    assert 'model?.savedMaxCompletionTokens ?? ""' in source
    assert 'model?.savedThinkingBudget ?? ""' in source

    # CSS:grid/flex 容器会盖过 UA 的 [hidden] 默认,body 与门控字段都需显式复位。
    assert ".workspace-advanced-body[hidden]" in styles
    assert ".workspace-advanced-body > .workspace-field[hidden]" in styles
    assert '.workspace-advanced-toggle[aria-expanded="true"] .workspace-advanced-chevron' in styles

    # api.js 以 undefined/int/null 三态发送(可清空),而非 effort/apiBase 的真值模式。
    assert "thinkingBudget" in api_source
    assert "maxCompletionTokens" in api_source
    assert "thinkingBudget !== undefined" in api_source
    assert "maxCompletionTokens !== undefined" in api_source


def test_api_exposes_workspace_operation_routes() -> None:
    source = _source(API_JS)

    for name in [
        "updateSession",
        "getSessionStatus",
        "getSessionDebug",
        "getSessionPrompt",
        "compactSession",
        "getProviders",
        "saveProviderConfig",
        "setActiveProvider",
        "clearProviderConfig",
        "getAliyunCloud",
        "saveAliyunCloud",
        "getMemory",
        "listMemoryProjects",
        "saveProjectMemory",
        "saveUserMemory",
        "saveAutoMemory",
        "searchLegacyMemory",
        "deleteLegacyMemory",
        "getSkills",
        "saveDisabledSkills",
        "getTranscriptTurn",
    ]:
        assert f"export function {name}" in source

    for route in [
        '"/status"',
        '"/debug"',
        '"/prompt"',
        '"/compact"',
        '"/api/providers"',
        '"/api/providers/config"',
        '"/api/providers/active"',
        '"/api/cloud/aliyun"',
        '"/api/memory"',
        '"/api/memory/projects"',
        '"/api/memory/project"',
        '"/api/memory/user"',
        '"/api/memory/auto"',
        '"/api/memory/legacy"',
        '"/api/skills"',
        '"/api/skills/disabled"',
        '"/api/transcript/"',
    ]:
        assert route in source

    assert "if (effort) {\n    payload.effort = effort;" in source
    assert "if (apiBase) {\n    payload.apiBase = apiBase;" in source


def test_api_exposes_aliyun_oauth_login_and_field_hint() -> None:
    api_source = _source(API_JS)
    workspace_source = _source(WORKSPACE_JS)

    assert "oauthLoginAliyun" in api_source
    assert "/api/cloud/aliyun/oauth-login" in api_source
    assert "workspace-field-hint" in workspace_source


def test_workspace_cloud_panel_isolates_modes_and_shows_oauth_site() -> None:
    workspace_source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    # #1:摘要携带 oauthSiteType,并在回填表单时写回「登录站点」选择框。
    assert "oauthSiteType: payload?.oauthSiteType || null" in workspace_source
    assert (
        "if (summaryPayload.oauthSiteType) cloudOauthSiteSelect.value = summaryPayload.oauthSiteType;"
        in workspace_source
    )

    # #2:地域卡片专属类放开 overflow,让区域下拉菜单完整浮出而非被裁成一条缝。
    assert "workspace-cloud-region-card" in workspace_source
    assert ".workspace-cloud-region-card {\n  overflow: visible;\n}" in styles
    # 区域菜单是 flex column,选项须 flex-shrink:0,否则地域一多会被压扁到文字重叠。
    assert "flex-shrink: 0;" in styles

    # #3:字段提示按当前鉴权模式过滤,避免 OAuth 派生的 STS 字段串味到 AK/StsToken 表单。
    assert 'if (d.mode && d.mode !== currentMode) return "";' in workspace_source


def test_workspace_cloud_panel_prefills_secrets_and_resets_on_mode_switch() -> None:
    workspace_source = _source(WORKSPACE_JS)
    styles = _source(STYLES_CSS)

    # #1:密钥字段像模型 API Key 一样可回填/查看——后端回传的原始值经快照 savedCloud 预填输入框。
    assert "let savedCloud = null;" in workspace_source
    assert "const applyCloudInputs = (mode) => {" in workspace_source
    assert 'cloudAccessKeySecretInput.value = match ? savedCloud.accessKeySecret || "" : "";' in workspace_source
    assert 'accessKeySecret: (payload && payload.accessKeySecret) || "",' in workspace_source
    # 明文密钥绝不能落入展示摘要(result <pre>);aliyunCloudSummary 不得携带 accessKeySecret。
    summary_start = workspace_source.index("function aliyunCloudSummary(payload) {")
    summary_body = workspace_source[summary_start : workspace_source.index("\n}", summary_start)]
    assert "accessKeySecret" not in summary_body

    # #2:展示当前已保存的认证方式。
    assert "workspace-cloud-mode-hint" in workspace_source
    assert "const renderModeHint = () => {" in workspace_source
    assert "Currently saved: {mode}" in workspace_source
    assert "No cloud credentials saved yet" in workspace_source

    # #3:切换认证方式即按目标模式重置/回填输入,不让上一模式的 AccessKey 残留。
    assert 'applyCloudInputs(selectedValue(cloudModeSelect) || "AK");' in workspace_source
    # 保存点击不再立刻清空密钥输入(保存失败也不丢用户填的值)。
    assert "clearCloudSecretInputs" not in workspace_source

    # #4:区域下拉菜单不再被 input 宽度锁死,按内容自适应变宽以显示完整地域值。
    assert "min-width: 100%;" in styles
    assert "width: max-content;" in styles
    assert "max-width: min(24rem, 80vw);" in styles


def test_app_wires_workspace_controls_to_current_session() -> None:
    source = _source(APP_JS)

    assert 'import { createWorkspaceController } from "./components/workspace.js?v=cloud-creds-v51";' in source
    assert "workspace = createWorkspaceController" in source
    assert 'tabs: byShell("workspace-tabs")' in source
    assert 'content: byShell("workspace-content")' in source
    assert "workspace?.setSession(state.currentSessionId, state.currentSession)" in source
    assert "workspace?.render(state)" in source
    # 归档面板取消归档/删除会话后须刷新主侧栏(否则被隐藏的空项目不会随取消归档重现)。
    assert "onSessionsMutated: async () => {" in source


def test_archived_panel_notifies_sidebar_after_session_mutation() -> None:
    source = _source(WORKSPACE_JS)

    # 控制器把外层回调挂到 context,供归档面板在取消归档/删除后通知主侧栏刷新。
    assert "export function createWorkspaceController({ tabs, content }, api, options = {}) {" in source
    assert "onSessionsMutated: () => options.onSessionsMutated?.()" in source
    # runAction 在成功刷新归档面板后触发外层侧栏刷新。
    assert "context.onSessionsMutated?.();" in source


def test_archived_group_delete_arms_inside_open_popover() -> None:
    source = _source(WORKSPACE_JS)

    # 项目「…」菜单的删除项:首次点击只 arm 成「确认删除?」并保持浮层展开,收起菜单
    # 必须在二次确认回调内(否则确认态被藏进已隐藏的菜单,看起来像点了没反应)。
    handler_start = source.index('deleteProjectItem.addEventListener("click"')
    confirm_call = source.index('armOrConfirm(deleteProjectItem, t("Confirm delete?")', handler_start)
    close_in_confirm = source.index("closeGroupMenu();", confirm_call)
    delete_call = source.index("api.deleteArchivedSessions(group.cwd)", confirm_call)
    # 收起菜单在 armOrConfirm 之后(其回调内),且早于真正的删除调用。
    assert confirm_call < close_in_confirm < delete_call
    # 不得在 armOrConfirm 之前抢先收起浮层。
    assert "closeGroupMenu();" not in source[handler_start:confirm_call]


def test_app_maps_command_results_to_workspace_tabs(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { commandWorkspaceTab } = await import(__APP_MODULE__);
            const commands = [
              { accepted: true, command: "auth", action: "open_settings", panel: "provider" },
              { accepted: true, command: "model", action: "open_model_selector" },
              { accepted: true, command: "effort", action: "open_effort_selector" },
              { accepted: true, command: "memory", action: "open_panel", panel: "memory" },
              { accepted: true, command: "skills", action: "open_panel", panel: "skills" },
              { accepted: true, command: "prompt", snapshot: { sections: [] } },
              { accepted: true, command: "compact", compacted: true },
            ];

            console.log(JSON.stringify({
              tabs: commands.map((command) => commandWorkspaceTab(command)),
              failure: commandWorkspaceTab({
                accepted: false,
                command: "auth",
                error: { code: "bad", message: "Nope" },
              }),
              compactFailure: commandWorkspaceTab({
                accepted: false,
                command: "compact",
                state: "too_short",
              }),
              unknown: commandWorkspaceTab({ accepted: true, command: "unknown" }),
            }));
            """
        ),
    )

    assert output == {
        # 末位 compact 结果(只带 command:"compact")不再打开废弃「状态」面板 → ""。
        "tabs": ["model", "model", "model", "memory", "skills", "status", ""],
        "failure": "status",
        # 失败/内容过短的压缩也不弹「状态」模态,交给内联「压缩结束」提示 → ""。
        "compactFailure": "",
        "unknown": "",
    }


def test_app_opens_workspace_modal_for_command_results(tmp_path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { commandOpensWorkspaceModal } = await import(__APP_MODULE__);

            console.log(JSON.stringify({
              auth: commandOpensWorkspaceModal({ accepted: true, command: "auth", action: "open_settings" }),
              memory: commandOpensWorkspaceModal({
                accepted: true,
                command: "memory",
                action: "open_panel",
                panel: "memory",
              }),
              status: commandOpensWorkspaceModal({ accepted: true, command: "status", status: {} }),
              unknown: commandOpensWorkspaceModal({ accepted: true, command: "unknown" }),
            }));
            """
        ),
    )

    assert output == {
        "auth": True,
        "memory": True,
        "status": False,
        "unknown": False,
    }


def test_app_renders_command_status_as_codex_like_inline_panel() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    assert 'data-app-shell="session-status-panel"' in html
    assert "renderInlineSessionStatusPanel" in app_source
    assert "showInlineSessionStatus" in app_source
    assert 'commandResult.command === "status"' in app_source
    assert "status.sessionId || status.webSessionId" in app_source
    assert 'byShell("session-status-panel")' in app_source
    assert ".session-status-panel" in styles
    assert ".session-status-card" in styles
    assert ".session-status-close" in styles
    assert ".session-status-row" in styles
    assert ".session-status-usage" in styles
    assert ".session-status-meter" in styles

    # 会话 ID 复制按钮:内联「状态」面板须有可点击复制图标(Issue: 状态界面缺复制按钮),
    # 复用 workspace 状态页同款 .workspace-status-copy 视觉,并写入剪贴板。
    assert "makeSessionStatusCopyButton" in app_source
    assert "copyStatusTextToClipboard" in app_source
    assert "navigator?.clipboard?.writeText" in app_source
    assert "workspace-status-copy session-status-copy" in app_source
    assert ".workspace-status-copy" in styles
    assert ".session-status-row-copy" in styles


def test_app_formats_status_usage_as_remaining_context(tmp_path: Path) -> None:
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { statusPanelRows, statusUsageText } = await import(__APP_MODULE__);
            const status = {
              sessionId: "019f0251-e019-70c3-98fc-25f861650ccd",
              usage: {
                inputTokens: 90000,
                outputTokens: 2950,
                totalTokens: 92950,
                recordedEvents: 4,
              },
              contextUsage: {
                totalTokens: 92950,
                contextWindow: 258000,
              },
              messageCounts: { visible: 47, resume: 10 },
            };

            console.log(JSON.stringify({
              usageText: statusUsageText(status),
              rows: statusPanelRows(status),
            }));
            """
        ),
    )

    assert output == {
        "usageText": "64% left (used 92,950 of 258K)",
        "rows": [
            {"label": "Session:", "value": "019f0251-e019-70c3-98fc-25f861650ccd", "copyable": True},
            {"label": "Context:", "value": "64% left (used 92,950 of 258K)"},
        ],
    }


def test_status_panel_rows_show_pipeline_step_context_windows(tmp_path: Path) -> None:
    # 流水线会话:/status 逐窗口展示与 composer 圈圈同源的每步上下文用量(文字版)。
    # 窗口 contextUsage 是后端 ContextManager.get_usage() 原样 dict(snake_case)、SSE 不改键名,
    # 故用 snake_case 构造以锁定 statusUsage 的 camel/snake 双读容错;标签沿用圈圈 tooltip 的
    # 「候选名 · 步骤名」拼法。无窗口(选择门 / reload 后不持久化)时退回单条会话级 Context 行。
    output = _run_app_script(
        tmp_path,
        textwrap.dedent(
            """
            globalThis.document = {
              getElementById() {
                return null;
              },
            };

            const { statusPanelRows } = await import(__APP_MODULE__);
            const status = {
              sessionId: "019f0251-e019-70c3-98fc-25f861650ccd",
              contextUsage: { total_tokens: 5000, context_window: 60000 },
            };
            const windows = [
              {
                groupId: "step:step-step1-1",
                title: "Draft template",
                candidateName: "",
                contextUsage: { total_tokens: 30000, context_window: 60000 },
              },
              {
                groupId: "candidate:cand-a",
                title: "Generate",
                candidateName: "Candidate A",
                contextUsage: { total_tokens: 12000, context_window: 60000 },
              },
            ];

            console.log(JSON.stringify({
              pipeline: statusPanelRows(status, { contextWindows: windows }),
              fallback: statusPanelRows(status, { contextWindows: [] }),
            }));
            """
        ),
    )

    assert output == {
        "pipeline": [
            {"label": "Session:", "value": "019f0251-e019-70c3-98fc-25f861650ccd", "copyable": True},
            {"label": "Context:", "value": "Draft template · 50% left (used 30,000 of 60K)"},
            {"label": "Context:", "value": "Candidate A · Generate · 80% left (used 12,000 of 60K)"},
        ],
        "fallback": [
            {"label": "Session:", "value": "019f0251-e019-70c3-98fc-25f861650ccd", "copyable": True},
            {"label": "Context:", "value": "92% left (used 5,000 of 60K)"},
        ],
    }


def test_app_does_not_reopen_status_panel_from_replayed_command_events() -> None:
    app_source = _source(APP_JS)

    assert 'event.type === "command.finished"' not in app_source
    assert "handleCommandResult(result = {})" in app_source
    assert 'commandResult.command === "status"' in app_source


def test_app_and_composer_apply_command_results_to_workspace() -> None:
    app_source = _source(APP_JS)
    composer_source = _source(COMPOSER_JS)

    assert "applyCommandResult" in app_source
    assert "handleCommandResult" in app_source
    assert "openWorkspaceModal(tab)" in app_source
    assert "onCommandResult: handleCommandResult" in app_source
    assert "options.onCommandResult" in composer_source


def test_app_wires_workspace_modal_and_config_button() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)
    styles = _source(STYLES_CSS)

    for hook in [
        'data-app-shell="workspace-open-config"',
        'data-app-shell="workspace-modal"',
        'data-app-shell="workspace-modal-backdrop"',
        'data-app-shell="workspace-modal-close"',
    ]:
        assert hook in html

    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert html.index('data-app-shell="workspace-modal"') < html.index('data-app-shell="workspace-tabs"')
    assert "openWorkspaceModal" in app_source
    assert "closeWorkspaceModal" in app_source
    assert (
        'byShell("workspace-open-config")?.addEventListener("click", () => openWorkspaceModal("settings"))'
        in app_source
    )
    assert 'byShell("workspace-modal-close")?.addEventListener("click", closeWorkspaceModal)' in app_source
    assert 'byShell("workspace-modal-backdrop")?.addEventListener("click", closeWorkspaceModal)' in app_source
    assert ".workspace-modal" in styles
    assert ".workspace-dialog" in styles
    assert ".workspace-config-button" in styles


def test_workspace_component_uses_safe_dom_rendering_for_api_payloads() -> None:
    source = _source(WORKSPACE_JS)

    assert ".innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert ".textContent" in source
    assert ".replaceChildren" in source
    for unsafe_sink in ["outerHTML", "DOMParser", "Range().createContextualFragment"]:
        assert unsafe_sink not in source


def test_workspace_component_calls_provider_memory_skills_and_search_apis() -> None:
    source = _source(WORKSPACE_JS)

    for call in [
        "api.getSessionStatus",
        "api.getSessionDebug",
        "api.getSessionPrompt",
        "api.compactSession",
        "api.getProviders",
        "api.saveProviderConfig",
        "api.setActiveProvider",
        "api.getAliyunCloud",
        "api.saveAliyunCloud",
        "api.getMemory",
        "api.listMemoryProjects",
        "api.saveProjectMemory",
        "api.saveUserMemory",
        "api.saveAutoMemory",
        "api.searchLegacyMemory",
        "api.deleteLegacyMemory",
        "api.getSkills",
        "api.saveDisabledSkills",
    ]:
        assert call in source

    for marker in [
        "workspace-settings-provider",
        "workspace-model-model",
        "workspace-model-effort",
        "workspace-model-api-key",
        "workspace-cloud-mode",
        "workspace-cloud-region",
        "workspace-cloud-access-key-id",
        "workspace-cloud-access-key-secret",
        "workspace-memory-project",
        "workspace-memory-project-select",
        "workspace-memory-user",
        "workspace-memory-auto",
        "workspace-memory-legacy-query",
        "workspace-memory-legacy-results",
        "workspace-skills-list",
    ]:
        assert marker in source


def test_workspace_memory_panel_loads_globally_and_saves_by_selected_project(tmp_path) -> None:
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
            }

            function dataKey(attributeName) {
              return attributeName
                .slice("data-".length)
                .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
            }

            function selectorMatches(node, selector) {
              const classMatch = selector.match(/^\\.([\\w-]+)$/);
              if (classMatch) {
                return (node.className || "").split(/\\s+/).includes(classMatch[1]);
              }
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              if (!(key in node.dataset)) {
                return false;
              }
              return expected === undefined || node.dataset[key] === expected;
            }

            function findByClass(node, cls) {
              if ((node.className || "").split(/\\s+/).includes(cls)) {
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

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
                if (name.startsWith("data-")) {
                  this.dataset[dataKey(name)] = String(value);
                }
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this });
                }
              }
              click() {
                this.dispatch("click");
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const flush = async () => {
              for (let i = 0; i < 6; i += 1) {
                await new Promise((resolve) => setTimeout(resolve, 0));
              }
            };

            const getMemoryCalls = [];
            const saves = [];
            const payloadFor = (cwd) => ({
              project: { content: cwd === "/proj/b" ? "B project" : "A project", path: cwd + "/AGENTS.md" },
              user: { content: "user memory", path: "/config/AGENTS.md" },
              autoMemoryEnabled: true,
              legacy: [],
            });
            const api = {
              listMemoryProjects() {
                return Promise.resolve({
                  projects: [
                    { cwd: "/proj/a", label: "a", current: true },
                    { cwd: "/proj/b", label: "b", current: false },
                  ],
                });
              },
              getMemory(arg) {
                getMemoryCalls.push(arg);
                const cwd = arg && arg.cwd ? arg.cwd : "/proj/a";
                return Promise.resolve(payloadFor(cwd));
              },
              saveProjectMemory(payload) {
                saves.push(["project", payload]);
                return Promise.resolve({ updated: true, path: payload.cwd + "/AGENTS.md" });
              },
              saveUserMemory(payload) {
                saves.push(["user", payload]);
                return Promise.resolve({ updated: true });
              },
              saveAutoMemory(enabled) {
                saves.push(["auto", enabled]);
                return Promise.resolve({ autoMemoryEnabled: enabled });
              },
              searchLegacyMemory() {
                return Promise.resolve({ memories: [] });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            // No session set: the memory panel must still load (regression for "没有活动会话").
            controller.setActiveTab("memory");
            await flush();

            const select = content.querySelector('[data-workspace-action="workspace-memory-project-select"]');
            const project = content.querySelector('[data-workspace-action="workspace-memory-project"]');
            const user = content.querySelector('[data-workspace-action="workspace-memory-user"]');
            const auto = content.querySelector('[data-workspace-action="workspace-memory-auto"]');
            const autoStatus = findByClass(content, "workspace-memory-auto-status");
            const saveProject = content.querySelector('[data-workspace-action="workspace-memory-save-project"]');
            const saveUser = content.querySelector('[data-workspace-action="workspace-memory-save-user"]');

            // Switch the selected project and reload just the project note.
            select.value = "/proj/b";
            select.dispatch("change");
            await flush();

            saveProject.click();
            saveUser.click();
            await flush();

            // Toggle auto-memory off; its status must resolve, not stay on "正在保存…".
            auto.checked = false;
            auto.dispatch("change");
            await flush();

            console.log(JSON.stringify({
              optionCount: select.options.length,
              getMemoryCalls,
              projectValue: project.value,
              userValue: user.value,
              saves,
              autoStatus: autoStatus.textContent,
            }));
            """
        ),
    )

    assert output == {
        "optionCount": 2,
        "getMemoryCalls": [{"cwd": "/proj/a"}, {"cwd": "/proj/b"}],
        "projectValue": "B project",
        "userValue": "user memory",
        "saves": [
            ["project", {"cwd": "/proj/b", "content": "B project"}],
            ["user", {"content": "user memory"}],
            ["auto", False],
        ],
        "autoStatus": "Automatic memory disabled",
    }


def test_workspace_mcp_caps_toggle_collapses_without_refetching(tmp_path) -> None:
    # 「收起能力」必须真正收起,不能因用建卡时捕获的 state 闭包快照把收起误算成再次展开。
    # 回归:点收起反而重新显示「正在连接并获取能力…」并再次展开(getMcpCapabilities 被重复调用)。
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
            }

            function dataKey(attributeName) {
              return attributeName
                .slice("data-".length)
                .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
            }

            function selectorMatches(node, selector) {
              const classMatch = selector.match(/^\\.([\\w-]+)$/);
              if (classMatch) {
                return (node.className || "").split(/\\s+/).includes(classMatch[1]);
              }
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              if (!(key in node.dataset)) {
                return false;
              }
              return expected === undefined || node.dataset[key] === expected;
            }

            function findByClass(node, cls) {
              if ((node.className || "").split(/\\s+/).includes(cls)) {
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

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
                if (name.startsWith("data-")) {
                  this.dataset[dataKey(name)] = String(value);
                }
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this });
                }
              }
              click() {
                this.dispatch("click");
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const flush = async () => {
              for (let i = 0; i < 6; i += 1) {
                await new Promise((resolve) => setTimeout(resolve, 0));
              }
            };

            const capsCalls = [];
            const api = {
              listProjects() {
                return Promise.resolve({ projects: [{ cwd: "/proj/a", label: "a", current: true }] });
              },
              getMcpServers() {
                return Promise.resolve({
                  servers: [
                    {
                      name: "coop",
                      scope: "local",
                      transport: "http",
                      url: "https://mcp.example.com/coop",
                      connection_state: "connected",
                      disabled: false,
                      source_path: "",
                    },
                  ],
                  warnings: [],
                });
              },
              getMcpCapabilities(arg) {
                capsCalls.push(arg);
                return Promise.resolve({ tools: [], resources: [], prompts: [], connection_state: "connected" });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setActiveTab("skills");
            content.querySelector('[data-plugins-subtab="mcp"]').click();
            await flush();

            const toggle = content.querySelector('[data-workspace-action="workspace-mcp-caps-toggle"]');
            const caps = findByClass(content, "workspace-mcp-caps");
            const initialLabel = toggle.textContent;

            toggle.click(); // 展开并拉取能力
            await flush();
            const afterExpand = { label: toggle.textContent, hidden: caps.hidden, calls: capsCalls.length };

            toggle.click(); // 收起,不应再次拉取
            await flush();
            const afterCollapse = { label: toggle.textContent, hidden: caps.hidden, calls: capsCalls.length };

            console.log(JSON.stringify({ initialLabel, afterExpand, afterCollapse }));
            """
        ),
    )

    assert output == {
        "initialLabel": "View capabilities",
        "afterExpand": {"label": "Hide capabilities", "hidden": False, "calls": 1},
        # 收起后按钮回到「查看能力」、能力区隐藏,且未再调用 getMcpCapabilities(仍为 1 次)。
        "afterCollapse": {"label": "View capabilities", "hidden": True, "calls": 1},
    }


def test_workspace_mcp_auth_buttons_persist_after_clearing_auth(tmp_path) -> None:
    # 远程 http 服务器即便走动态客户端注册(无 oauth 段、离线也无 configured/stored client_id、
    # auth_state=not-configured),也须提供「认证/清除认证」按钮并显示「认证:未进行」状态
    # (回归:按钮与状态错误消失)。本地 stdio 服务器不涉及认证,不显示按钮与认证状态。
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
            }

            function dataKey(attributeName) {
              return attributeName
                .slice("data-".length)
                .replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
            }

            function selectorMatches(node, selector) {
              const classMatch = selector.match(/^\\.([\\w-]+)$/);
              if (classMatch) {
                return (node.className || "").split(/\\s+/).includes(classMatch[1]);
              }
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              if (!(key in node.dataset)) {
                return false;
              }
              return expected === undefined || node.dataset[key] === expected;
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
                if (name.startsWith("data-")) {
                  this.dataset[dataKey(name)] = String(value);
                }
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              dispatch(type) {
                for (const handler of this.listeners[type] || []) {
                  handler({ type, target: this });
                }
              }
              click() {
                this.dispatch("click");
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const flush = async () => {
              for (let i = 0; i < 6; i += 1) {
                await new Promise((resolve) => setTimeout(resolve, 0));
              }
            };

            const api = {
              listProjects() {
                return Promise.resolve({ projects: [{ cwd: "/proj/a", label: "a", current: true }] });
              },
              getMcpServers() {
                return Promise.resolve({
                  servers: [
                    {
                      name: "coop",
                      scope: "local",
                      transport: "http",
                      url: "https://mcp.example.com/coop",
                      connection_state: "connected",
                      auth_state: "not-configured",
                      disabled: false,
                      source_path: "",
                      oauth_client_state: {
                        oauth_configured: false,
                        configured_client_id: false,
                        stored_client_id: false,
                        stored_client_secret: false,
                      },
                    },
                    {
                      name: "local-fs",
                      scope: "local",
                      transport: "stdio",
                      command: "npx server",
                      connection_state: "connected",
                      auth_state: "not-configured",
                      disabled: false,
                      source_path: "",
                      oauth_client_state: {
                        oauth_configured: false,
                        configured_client_id: false,
                        stored_client_id: false,
                        stored_client_secret: false,
                      },
                    },
                  ],
                  warnings: [],
                });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setActiveTab("skills");
            content.querySelector('[data-plugins-subtab="mcp"]').click();
            await flush();

            const authButtons = content.querySelectorAll('[data-workspace-action="workspace-mcp-auth-start"]');
            const resetButtons = content.querySelectorAll('[data-workspace-action="workspace-mcp-reset-auth"]');
            const authStates = content.querySelectorAll(".workspace-mcp-auth");

            console.log(JSON.stringify({
              authCount: authButtons.length,
              resetCount: resetButtons.length,
              authLabel: authButtons.length ? authButtons[0].textContent : null,
              authStates: authStates.map((node) => node.textContent),
            }));
            """
        ),
    )

    # 动态注册的远程 coop 显示认证/清除认证并标注「认证:未进行」;stdio 服务器无按钮、无认证状态。
    # 未认证时按钮文案为「认证」(非「重新认证」)。
    assert output == {
        "authCount": 1,
        "resetCount": 1,
        "authLabel": "Authenticate",
        "authStates": ["Auth: not performed"],
    }


def test_workspace_mcp_state_labels_are_localized() -> None:
    # 回归:auth_state=configured 与 connection_state=skipped 缺中文映射时会直接回退成英文
    # (`认证:configured`、`skipped` 徽标)。后端 _auth_state / _health_status_for_state
    # 能产出的取值都必须在对应的 JS 标签表里有键。
    source = _source(WORKSPACE_JS)
    auth_block = re.search(r"const MCP_AUTH_LABELS = \{(.*?)\};", source, re.DOTALL)
    conn_block = re.search(r"const MCP_CONNECTION_LABELS = \{(.*?)\};", source, re.DOTALL)
    assert auth_block is not None and conn_block is not None
    auth_labels = auth_block.group(1)
    conn_labels = conn_block.group(1)

    assert 'configured: t("Configured")' in auth_labels
    assert 'skipped: t("Not checked")' in conn_labels

    # 后端 _auth_state 的全部返回值(manager.py:_auth_state)。
    for key in ("authenticated", "configured", '"needs-auth"', '"not-configured"', "error"):
        assert key in auth_labels, key
    # 后端 _health_status_for_state 的全部返回值(manager.py)。
    for key in ("connected", '"needs-auth"', "failed", "disabled", "skipped"):
        assert key in conn_labels, key


def test_workspace_cloud_save_uses_secret_inputs_and_redacted_summary(tmp_path) -> None:
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
              add(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.add(name);
                this.owner.className = [...items].join(" ");
              }
              remove(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.delete(name);
                this.owner.className = [...items].join(" ");
              }
            }

            function selectorMatches(node, selector) {
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            function textOf(node) {
              return `${node.textContent || ""} ${(node.children || []).map(textOf).join(" ")}`.trim();
            }

            function required(selector, root) {
              const node = root.querySelector(selector);
              if (!node) {
                throw new Error(`missing ${selector}`);
              }
              return node;
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const saves = [];
            const api = {
              getProviders() {
                return Promise.resolve({
                  active: { provider: "openai", model: "gpt-5.5", effort: "high", apiBase: null, hasApiKey: false },
                  providers: [{ key: "openai", name: "OpenAI", models: [{ id: "gpt-5.5", efforts: ["high"] }] }],
                });
              },
              getAliyunCloud() {
                return Promise.resolve({ configured: false, mode: null, region: null, expiration: null });
              },
              saveAliyunCloud(payload) {
                saves.push(payload);
                return Promise.resolve({
                  configured: true,
                  mode: payload.mode,
                  region: payload.region,
                  expiration: null,
                  accessKeySecret: "leaked-cloud-secret",
                  stsToken: "leaked-sts-token",
                });
              },
              saveActiveProvider() {
                return Promise.resolve({ active: {} });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setSession("A", { webSessionId: "A", mode: "normal" });
            controller.setActiveTab("cloud");
            await new Promise((resolve) => setTimeout(resolve, 0));

            required('[data-workspace-action="workspace-cloud-mode"]', content).value = "AK";
            required('[data-workspace-action="workspace-cloud-region"]', content).value = "cn-shanghai";
            required('[data-workspace-action="workspace-cloud-access-key-id"]', content).value = "LTAI-fake";
            const secret = required('[data-workspace-action="workspace-cloud-access-key-secret"]', content);
            secret.value = "fake-cloud-secret";
            required('[data-workspace-action="workspace-cloud-save"]', content).click();
            await new Promise((resolve) => setTimeout(resolve, 0));

            console.log(JSON.stringify({
              saves,
              secretType: secret.attributes.type,
              secretValue: secret.value,
              renderedText: textOf(content),
            }));
            """
        ),
    )

    assert output["saves"] == [
        {
            "mode": "AK",
            "region": "cn-shanghai",
            "accessKeyId": "LTAI-fake",
            "accessKeySecret": "fake-cloud-secret",
            "stsToken": "",
            "stsExpiration": "",
            "ramRoleArn": "",
            "ramSessionName": "",
            "oauthSiteType": "",
            "oauthAccessToken": "",
            "oauthRefreshToken": "",
            "oauthAccessTokenExpire": "",
            "oauthRefreshTokenExpire": "",
        }
    ]
    # 保存后与模型 API Key 一致:密钥回填到输入框(默认以密文 password 呈现,可通过眼睛按钮查看),
    # 但绝不进入可见的摘要/渲染文本。
    assert output["secretType"] == "password"
    assert output["secretValue"] == "leaked-cloud-secret"
    assert "leaked-cloud-secret" not in str(output["renderedText"])
    assert "leaked-sts-token" not in str(output["renderedText"])
    assert "fake-cloud-secret" not in str(output["renderedText"])
    assert "error" not in str(output["renderedText"]).lower()
    assert "cn-shanghai" in str(output["renderedText"])


def test_workspace_cloud_save_gates_stale_secrets_after_mode_switch(tmp_path) -> None:
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
              add(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.add(name);
                this.owner.className = [...items].join(" ");
              }
              remove(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.delete(name);
                this.owner.className = [...items].join(" ");
              }
            }

            function selectorMatches(node, selector) {
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            function required(selector, root) {
              const node = root.querySelector(selector);
              if (!node) {
                throw new Error(`missing ${selector}`);
              }
              return node;
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const saves = [];
            const api = {
              getProviders() {
                return Promise.resolve({
                  active: { provider: "openai", model: "gpt-5.5", effort: "high", apiBase: null, hasApiKey: false },
                  providers: [{ key: "openai", name: "OpenAI", models: [{ id: "gpt-5.5", efforts: ["high"] }] }],
                });
              },
              getAliyunCloud() {
                return Promise.resolve({ configured: false, mode: null, region: null, expiration: null });
              },
              saveAliyunCloud(payload) {
                saves.push(payload);
                return Promise.resolve({
                  configured: true,
                  mode: payload.mode,
                  region: payload.region,
                  expiration: null,
                });
              },
              saveActiveProvider() {
                return Promise.resolve({ active: {} });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setSession("A", { webSessionId: "A", mode: "normal" });
            controller.setActiveTab("cloud");
            await new Promise((resolve) => setTimeout(resolve, 0));

            // 先在 AK 模式填入敏感值,再切到 OAuth——持久(未挂载)的输入框仍持有旧 AK 明文
            required('[data-workspace-action="workspace-cloud-mode"]', content).value = "AK";
            required('[data-workspace-action="workspace-cloud-region"]', content).value = "cn-shanghai";
            required('[data-workspace-action="workspace-cloud-access-key-id"]', content).value = "LTAI-fake";
            const secret = required('[data-workspace-action="workspace-cloud-access-key-secret"]', content);
            secret.value = "fake-cloud-secret";
            required('[data-workspace-action="workspace-cloud-mode"]', content).value = "OAuth";
            required('[data-workspace-action="workspace-cloud-save"]', content).click();
            await new Promise((resolve) => setTimeout(resolve, 0));

            console.log(JSON.stringify({ saves }));
            """
        ),
    )

    saves = output["saves"]
    assert isinstance(saves, list) and len(saves) == 1
    payload = saves[0]
    assert payload["mode"] == "OAuth"
    assert payload["accessKeyId"] == ""
    assert payload["accessKeySecret"] == ""
    assert payload["stsToken"] == ""
    assert payload["stsExpiration"] == ""
    assert payload["ramRoleArn"] == ""
    assert payload["ramSessionName"] == ""
    # 恰好 13 个键
    assert len(payload) == 13
    # 旧的明文 AK 不得出现在任何字段中
    assert "fake-cloud-secret" not in str(payload)
    assert "LTAI-fake" not in str(payload)


def test_workspace_cloud_region_accepts_manual_free_text(tmp_path) -> None:
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
              add(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.add(name);
                this.owner.className = [...items].join(" ");
              }
              remove(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.delete(name);
                this.owner.className = [...items].join(" ");
              }
            }

            function selectorMatches(node, selector) {
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            function required(selector, root) {
              const node = root.querySelector(selector);
              if (!node) {
                throw new Error(`missing ${selector}`);
              }
              return node;
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const saves = [];
            const api = {
              getProviders() {
                return Promise.resolve({
                  active: { provider: "openai", model: "gpt-5.5", effort: "high", apiBase: null, hasApiKey: false },
                  providers: [{ key: "openai", name: "OpenAI", models: [{ id: "gpt-5.5", efforts: ["high"] }] }],
                });
              },
              getAliyunCloud() {
                return Promise.resolve({ configured: false, mode: null, region: null, expiration: null });
              },
              saveAliyunCloud(payload) {
                saves.push(payload);
                return Promise.resolve({
                  configured: true, mode: payload.mode, region: payload.region, expiration: null
                });
              },
              saveActiveProvider() {
                return Promise.resolve({ active: {} });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setSession("A", { webSessionId: "A", mode: "normal" });
            controller.setActiveTab("cloud");
            await new Promise((resolve) => setTimeout(resolve, 0));

            const regionEl = required('[data-workspace-action="workspace-cloud-region"]', content);
            required('[data-workspace-action="workspace-cloud-mode"]', content).value = "AK";
            // 手填一个不在候选列表内的地域,验证组合框允许自由输入。
            regionEl.value = "cn-not-in-list";
            required('[data-workspace-action="workspace-cloud-access-key-id"]', content).value = "LTAI-fake";
            required(
              '[data-workspace-action="workspace-cloud-access-key-secret"]', content
            ).value = "fake-cloud-secret";
            required('[data-workspace-action="workspace-cloud-save"]', content).click();
            await new Promise((resolve) => setTimeout(resolve, 0));

            console.log(JSON.stringify({
              regionTag: regionEl.tagName,
              savedRegion: saves[0] ? saves[0].region : null,
            }));
            """
        ),
    )

    # 区域控件现为可自由输入的 input(组合框),而非 <select>——枚举下拉无法手填。
    assert output["regionTag"] == "INPUT"
    # 手填的、不在候选列表内的地域应原样进入保存载荷。
    assert output["savedRegion"] == "cn-not-in-list"


def test_workspace_cloud_oauth_cancel_restores_form(tmp_path) -> None:
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
              add(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.add(name);
                this.owner.className = [...items].join(" ");
              }
              remove(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.delete(name);
                this.owner.className = [...items].join(" ");
              }
            }

            function selectorMatches(node, selector) {
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            function textOf(node) {
              return `${node.textContent || ""} ${(node.children || []).map(textOf).join(" ")}`.trim();
            }

            function required(selector, root) {
              const node = root.querySelector(selector);
              if (!node) {
                throw new Error(`missing ${selector}`);
              }
              return node;
            }

            function fireChange(node) {
              for (const handler of node.listeners.change || []) {
                handler({ type: "change", target: node });
              }
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            const api = {
              getProviders() {
                return Promise.resolve({
                  active: { provider: "openai", model: "gpt-5.5", effort: "high", apiBase: null, hasApiKey: false },
                  providers: [{ key: "openai", name: "OpenAI", models: [{ id: "gpt-5.5", efforts: ["high"] }] }],
                });
              },
              getAliyunCloud() {
                return Promise.resolve({ configured: false, mode: null, region: null, expiration: null });
              },
              // 永不 resolve:模拟浏览器已打开但用户未完成登录(甚至直接关闭浏览器)。
              oauthLoginAliyun() {
                return new Promise(() => {});
              },
              saveActiveProvider() {
                return Promise.resolve({ active: {} });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setSession("A", { webSessionId: "A", mode: "normal" });
            controller.setActiveTab("cloud");
            await new Promise((resolve) => setTimeout(resolve, 0));

            const modeSel = required('[data-workspace-action="workspace-cloud-mode"]', content);
            modeSel.value = "OAuth";
            fireChange(modeSel);

            const loginBtn = required('[data-workspace-action="workspace-cloud-oauth-login"]', content);
            const cancelBtn = required('[data-workspace-action="workspace-cloud-oauth-cancel"]', content);
            const before = { cancelHidden: cancelBtn.hidden, loginDisabled: loginBtn.disabled };
            loginBtn.click();
            const during = { cancelHidden: cancelBtn.hidden, loginDisabled: loginBtn.disabled };
            cancelBtn.click();
            const after = { cancelHidden: cancelBtn.hidden, loginDisabled: loginBtn.disabled };

            console.log(JSON.stringify({ before, during, after, text: textOf(content) }));
            """
        ),
    )

    # 初始:登录可用、取消隐藏。
    assert output["before"] == {"cancelHidden": True, "loginDisabled": False}
    # 登录进行中:登录禁用、取消可见(给用户退出等待的入口)。
    assert output["during"] == {"cancelHidden": False, "loginDisabled": True}
    # 取消后:界面复位,不再卡死;登录重新可用、取消再次隐藏。
    assert output["after"] == {"cancelHidden": True, "loginDisabled": False}
    assert "Login canceled." in str(output["text"])


def test_api_cloud_region_only_save_omits_empty_secret_fields(tmp_path) -> None:
    output = _run_api_script(
        tmp_path,
        textwrap.dedent(
            """
            import { saveAliyunCloud } from __API_MODULE__;

            const calls = [];
            globalThis.fetch = async (url, options) => {
              calls.push({ url, body: JSON.parse(options.body) });
              return {
                ok: true,
                headers: { get: () => "application/json" },
                json: async () => ({ configured: true, mode: "AK", region: "cn-beijing", expiration: null }),
              };
            };

            await saveAliyunCloud({
              mode: "AK",
              region: "cn-beijing",
              accessKeyId: "",
              accessKeySecret: "",
              stsToken: "",
              ramRoleArn: "",
              ramSessionName: "",
              oauthSiteType: "",
              oauthAccessToken: "",
              oauthRefreshToken: "",
              oauthAccessTokenExpire: "",
              oauthRefreshTokenExpire: "",
            });

            console.log(JSON.stringify(calls[0]));
            """
        ),
    )

    assert output == {"url": "/api/cloud/aliyun", "body": {"mode": "AK", "region": "cn-beijing"}}


def test_workspace_legacy_memory_search_and_delete_use_dom_controls(tmp_path) -> None:
    output = _run_workspace_script(
        tmp_path,
        textwrap.dedent(
            """
            import { createWorkspaceController } from __WORKSPACE_MODULE__;

            class ClassList {
              constructor(owner) {
                this.owner = owner;
              }
              toggle(name, force) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                if (force) {
                  items.add(name);
                } else {
                  items.delete(name);
                }
                this.owner.className = [...items].join(" ");
              }
              add(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.add(name);
                this.owner.className = [...items].join(" ");
              }
              remove(name) {
                const items = new Set((this.owner.className || "").split(/\\s+/).filter(Boolean));
                items.delete(name);
                this.owner.className = [...items].join(" ");
              }
            }

            function selectorMatches(node, selector) {
              const match = selector.match(/^(?:(\\w+))?\\[data-([\\w-]+)(?:="([^"]*)")?\\]$/);
              if (!match) {
                return false;
              }
              const [, tagName, dataName, expected] = match;
              if (tagName && node.tagName !== tagName.toUpperCase()) {
                return false;
              }
              const key = dataName.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
              return key in node.dataset && (expected === undefined || node.dataset[key] === expected);
            }

            class Element {
              constructor(tagName) {
                this.tagName = tagName.toUpperCase();
                this.children = [];
                this.dataset = {};
                this.attributes = {};
                this.listeners = {};
                this.textContent = "";
                this.className = "";
                this.value = "";
                this.checked = false;
                this.disabled = false;
                this.hidden = false;
                this.classList = new ClassList(this);
              }
              append(...children) {
                this.children.push(...children);
              }
              replaceChildren(...children) {
                this.children = children;
              }
              setAttribute(name, value) {
                this.attributes[name] = String(value);
              }
              addEventListener(type, handler) {
                this.listeners[type] = [...(this.listeners[type] || []), handler];
              }
              click() {
                for (const handler of this.listeners.click || []) {
                  handler({ type: "click", target: this });
                }
              }
              querySelectorAll(selector) {
                const matches = [];
                const visit = (node) => {
                  if (selectorMatches(node, selector)) {
                    matches.push(node);
                  }
                  for (const child of node.children || []) {
                    visit(child);
                  }
                };
                visit(this);
                return matches;
              }
              querySelector(selector) {
                return this.querySelectorAll(selector)[0] || null;
              }
              get options() {
                return this.children;
              }
            }

            function textOf(node) {
              return `${node.textContent || ""} ${(node.children || []).map(textOf).join(" ")}`.trim();
            }

            const tabs = new Element("nav");
            const content = new Element("div");
            globalThis.document = {
              createElement(tagName) {
                return new Element(tagName);
              },
            };

            let deleted = false;
            const deleteCalls = [];
            const api = {
              getMemory() {
                return Promise.resolve({
                  project: { content: "" },
                  user: { content: "" },
                  autoMemoryEnabled: true,
                  legacy: [],
                });
              },
              saveProjectMemory() {
                return Promise.resolve({ updated: true });
              },
              saveUserMemory() {
                return Promise.resolve({ updated: true });
              },
              saveAutoMemory() {
                return Promise.resolve({ autoMemoryEnabled: true });
              },
              searchLegacyMemory(query) {
                return Promise.resolve({
                  memories: deleted
                    ? []
                    : [{ memoryId: "alpha", name: "alpha", summary: `${query} summary`, type: "user" }],
                });
              },
              deleteLegacyMemory(memoryId) {
                deleteCalls.push(memoryId);
                deleted = true;
                return Promise.resolve({ deleted: true, memoryId });
              },
            };

            const controller = createWorkspaceController({ tabs, content }, api);
            controller.setSession("A", { webSessionId: "A", mode: "normal" });
            controller.setActiveTab("memory");
            await new Promise((resolve) => setTimeout(resolve, 0));
            content.querySelector('[data-workspace-action="workspace-memory-legacy-query"]').value = "alpha";
            content.querySelector('[data-workspace-action="workspace-memory-legacy-search"]').click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const beforeDelete = textOf(content);
            // 行内二次确认:首次点击只进入「确认删除?」态,不应触发删除。
            const deleteButton = content.querySelector('[data-legacy-memory-id="alpha"]');
            deleteButton.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const afterArm = { text: deleteButton.textContent, deleteCalls: deleteCalls.slice() };
            // 再次点击才真正删除。
            deleteButton.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const afterDelete = textOf(content);

            console.log(JSON.stringify({
              beforeDelete,
              afterArm,
              afterDelete,
              deleteCalls,
            }));
            """
        ),
    )

    assert "alpha summary" in str(output["beforeDelete"])
    assert output["afterArm"]["deleteCalls"] == []
    assert "Confirm delete?" in str(output["afterArm"]["text"])
    assert output["deleteCalls"] == ["alpha"]
    assert "alpha summary" not in str(output["afterDelete"])


def test_pipeline_workspace_component_renders_pipeline_sections() -> None:
    source = _source(PIPELINE_JS)

    assert "export function renderPipelineWorkspace" in source
    for state_field in [
        "pipelineSnapshot",
        "pipelineEvents",
        "candidateDetails",
        "diagrams",
        "cleanupStatus",
    ]:
        assert state_field in source
    for marker in [
        "pipeline-workspace-empty",
        "pipeline-diagnostics",
        "pipeline-stepper",
        "pipeline-candidates",
        "pipeline-diagram",
        "pipeline-progress",
        "pipeline-cleanup",
        "pipeline-handoff",
        "pipeline-error",
    ]:
        assert marker in source
    for payload_key in [
        "stack.progress",
        "stack.instances.progress",
        "candidate.selected",
        "pipeline.interrupt.judged",
        "pipeline_handoff_ready",
        "cleanup_started",
        "cleanup_progress",
        "cleanup_completed",
        "cleanup_failed",
        "contextId",
        "taskId",
        "lastSequence",
        "pipelineName",
        "candidateName",
        "candidateIndex",
        "totalMonthlyCost",
        "mermaidSource",
        "templateContent",
        "deploymentSucceeded",
        "deploymentComplete",
        "stackStatus",
        "progressStatus",
        "targetNormalMode",
    ]:
        assert payload_key in source


def test_pipeline_workspace_consumes_recovered_a2a_snapshot_fields() -> None:
    source = _source(PIPELINE_JS)

    for recovered_path in [
        "state.pipelineSnapshot?.display?.candidateDetails",
        "state.pipelineSnapshot?.display?.diagrams",
        "state.pipelineSnapshot?.steps",
        "state.pipelineSnapshot?.stacks",
        "state.pipelineSnapshot?.normalHandoff",
    ]:
        assert recovered_path in source
    for helper in [
        "combinedCandidates",
        "combinedDiagrams",
        "stepperItems",
        "snapshotProgressEvents",
        "normalHandoff",
    ]:
        assert helper in source
    assert "step.candidates" in source
    assert "candidate.detail && typeof candidate.detail" in source
    assert "stacks.byId" in source
    assert "stacks.current" in source
    assert "stacks.history" in source


def test_pipeline_workspace_surfaces_recovered_display_and_control_activity() -> None:
    source = _source(PIPELINE_JS)

    for recovered_path in [
        "state.pipelineSnapshot?.display?.artifacts",
        "state.pipelineSnapshot?.display?.permissions",
        "state.pipelineSnapshot?.display?.toolResults",
        "state.pipelineSnapshot?.pendingInput",
        "state.pipelineSnapshot?.control?.rollbackHistory",
        "state.pipelineSnapshot?.control?.candidateRestarts",
        "state.pipelineSnapshot?.control?.warningHistory",
    ]:
        assert recovered_path in source
    for marker in [
        "pipeline-recovery",
        "pipeline-recovery-grid",
        "pipeline-recovery-entry",
        "recoveredActivityItems",
        "renderRecoveryActivity",
        "appendRecoveryGroup",
    ]:
        assert marker in source


def test_legacy_workspace_search_panel_is_removed() -> None:
    workspace_source = _source(WORKSPACE_JS)
    html = _source(INDEX_HTML)
    assert "createSearchPanel" not in workspace_source
    assert 'panelControllers.set("search"' not in workspace_source
    assert "renderQuickOpenResults" not in workspace_source
    assert "compactResultLabel" not in workspace_source
    assert "compactResultMeta" not in workspace_source
    assert "compactResultSnippet" not in workspace_source
    assert 'data-workspace-panel="search"' not in html


def test_api_exposes_search_sessions_and_drops_legacy_search() -> None:
    api_source = _source(API_JS)
    assert "export function searchSessions" in api_source
    assert "/api/sessions/search" in api_source
    assert "export function searchFiles" not in api_source
    assert "export function quickOpenFiles" not in api_source
    assert "export function searchHistory" not in api_source


def test_command_palette_is_chat_spotlight() -> None:
    html = _source(INDEX_HTML)
    app_source = _source(APP_JS)

    # 文案:去掉与 placeholder 重复的标题栏,「搜索聊天或运行命令」只作为搜索框 placeholder 可见。
    assert 'placeholder="Search chats or run a command"' in html
    assert 'class="command-palette-header"' not in html
    assert 'id="command-palette-title"' not in html

    # 侧栏搜索按钮改指向命令面板(不再打开 workspace 的 search tab)。
    assert 'byShell("sidebar-search")?.addEventListener("click", openCommandPalette)' in app_source
    assert 'openWorkspaceModal("search")' not in app_source

    # spotlight 核心:异步刷新 + 竞态令牌 + 会话搜索 + 分组渲染 + ⌘数字。
    assert "refreshPalette" in app_source
    assert "paletteSearchToken" in app_source
    assert "api.searchSessions" in app_source
    assert "renderPaletteGroups" in app_source
    assert "paletteResults" in app_source

    # COMMAND_PALETTE_ITEMS 不再含自指的 search 项。
    assert 'tab: "search"' not in app_source

    # 「设置」拆成常规配置/模型/云凭证三条命令;流水线、状态不再作为命令项。
    assert 'label: t("General configuration")' in app_source
    assert 'tab: "other"' in app_source
    assert 'label: t("Models")' in app_source
    assert 'tab: "model"' in app_source
    assert 'label: t("Cloud credentials")' in app_source
    assert 'tab: "cloud"' in app_source
    assert 'label: "设置"' not in app_source
    assert 'label: "流水线", detail: "查看候选方案' not in app_source
    assert 'label: "状态", detail: "查看当前会话' not in app_source

    # 聊天分组最多 9 条(对应 ⌘1–9)。
    assert "PALETTE_CHAT_LIMIT = 9" in app_source
    assert "chats.slice(0, PALETTE_CHAT_LIMIT)" in app_source

    # 只有「聊天 / 未读聊天」两组(无「进行中」组,进行中的会话在聊天组内以行内转圈表示)。
    assert "function renderPaletteGroups({ chats, unread, commands })" in app_source
    assert '{ title: t("Unread chats"), sessions: unread }' in app_source
    assert "unread: unread.slice(0, PALETTE_CHAT_LIMIT)" in app_source
    assert '{ title: "进行中", sessions: running }' not in app_source

    # 搜索面板的会话行也显示未读圆点(与侧栏同判定)。
    assert 'const showUnread = Boolean(session.unread) && !isActiveRow && activity === "";' in app_source

    # 会话状态由后台轮询同步到打开中的搜索面板(进行中→未读的迁移能即时反映)。
    assert "function isCommandPaletteOpen()" in app_source
    assert "if (isCommandPaletteOpen())" in app_source

    # 项目名移到会话名下方:project 追加进 body(不再进右侧 right 列)。
    assert "body.append(project)" in app_source
    assert "right.append(project)" not in app_source

    # 会话行复用侧栏 .thread-mode-icon 字形,普通/流水线图标才可见(否则无尺寸/字形)。
    assert "command-palette-mode-icon thread-mode-icon" in app_source

    # 搜索框右侧显示 ⌘K 提示(对应全局 Cmd+K 打开面板的快捷键)。
    assert 'class="command-palette-search-hint" aria-hidden="true">⌘K<' in html

    # 新对话命令项取消 ⌘N 芯片,且移除全局 Cmd+N 监听(与浏览器自身 Cmd+N 冲突)。
    assert 'shortcut: "⌘N"' not in app_source
    assert 'event.key.toLowerCase() === "n" && (event.metaKey || event.ctrlKey)' not in app_source


def test_command_palette_layout_refinements() -> None:
    """搜索面板三处观感修复:标题字重、右列不拉伸芯片、列表可滚动。"""
    styles = _source(STYLES_CSS)

    # 会话名/命令名统一常规字重(不再 <strong> 默认 700,也不再 500 显粗)。
    assert ".command-palette-item strong {" in styles
    assert "font-weight: 400;" in styles
    # 去掉标题栏后,列表行取 minmax(0,1fr) 才能在 max-height 内滚动(dialog 变两行网格)。
    assert "grid-template-rows: auto minmax(0, 1fr);" in styles
    # 提高特异性(0-2-0)压过 `.command-palette-item > span`,右列恢复横向 flex、芯片自然宽。
    assert ".command-palette-item .command-palette-session-right {" in styles
    # 未读圆点在搜索面板行的专属尺寸/配色(基础尺寸只在 .thread-item 网格里定义)。
    assert ".command-palette-item .session-unread-dot {" in styles

    # 照 Codex 收敛字体:会话名 14px、分组标签中等字重(500)。
    assert "font-size: 0.875rem;" in styles

    # 搜索框右侧 ⌘K 提示:绝对定位居中,搜索框须 position:relative + input 右侧留白。
    assert ".command-palette-search-hint {" in styles
    assert ".command-palette-search {\n  position: relative;" in styles
    # 列表隐藏滚动条但保留滚动能力(Firefox + WebKit)。
    assert "scrollbar-width: none;" in styles
    assert ".command-palette-list::-webkit-scrollbar {" in styles


def test_foreign_sessions_frontend_wired() -> None:
    # 「常规」设置面板(配置组,tab id 仍为 other):api.js 数据层 + workspace.js 面板 +
    # index.html 静态壳,三处缺一不可,否则外来会话开关无法保存/回填或侧栏无法刷新。
    api_js = _source(API_JS)
    assert "getForeignSessionsVisibility" in api_js
    assert "saveForeignSessionsVisibility" in api_js
    assert "/api/settings/foreign-sessions" in api_js

    workspace_js = _source(WORKSPACE_JS)
    assert "createOtherPanel" in workspace_js
    assert 'panelControllers.set("other"' in workspace_js
    assert "Show foreign pipeline sessions (read-only)" in workspace_js
    assert "Show foreign normal sessions (resumable)" in workspace_js

    index_html = _source(INDEX_HTML)
    assert 'data-workspace-tab="other"' in index_html
    assert 'data-workspace-panel="other"' in index_html


def test_pipeline_review_step_frontend_wired() -> None:
    # 售卖流水线「审查步骤」开关:api.js 数据层 + workspace.js「常规」面板控件,
    # 缺任一处开关都无法保存/回填。后端路由由 tests/web/test_pipeline_review_step.py 覆盖。
    api_js = _source(API_JS)
    assert "getSellingReviewStep" in api_js
    assert "saveSellingReviewStep" in api_js
    assert "/api/settings/pipeline-review-step" in api_js

    workspace_js = _source(WORKSPACE_JS)
    assert 'makeForeignSwitch("workspace-pipeline-review-step")' in workspace_js
    assert "Enable review step" in workspace_js
    assert "persistReviewStep" in workspace_js
    assert "api.getSellingReviewStep()" in workspace_js
    assert "api.saveSellingReviewStep(" in workspace_js


def test_foreign_read_only_affordances() -> None:
    composer_js = _source(COMPOSER_JS)
    assert "setReadOnly" in composer_js

    app_js = _source(APP_JS)
    assert "setReadOnly" in app_js
    assert "readOnly" in app_js

    index_html = _source(INDEX_HTML)
    assert 'data-app-shell="read-only-banner"' in index_html

    styles = _source(STYLES_CSS)
    assert "thread-readonly-badge" in styles
    assert "read-only-banner" in styles


def test_app_js_renders_compaction_boundary() -> None:
    app_js = _source(APP_JS)
    assert "renderCompactionBoundaryMarker" in app_js
    assert 'message.kind === "context_compaction_boundary"' in app_js


def test_app_js_end_of_turn_compaction_boundary_renders_top_level() -> None:
    # 回归 44cd9909:回合结束后的压缩边界(会话末尾手动 /compact)不得折进收起的「已处理」组,
    # 而应在回合下方顶层画出可见分隔线。判据:向后跳过连续的压缩边界,看其后第一条“实际”消息;
    # 仅当那是同一回合的助手消息(真正的回合中途自动压缩,含一回合内连发多次)时才登记进
    # pendingTurn.boundaries;末尾/新回合用户消息/各类标记则走顶层原逻辑。
    app_js = _source(APP_JS)
    assert "compactionIsMidTurn" in app_js
    # 必须向后跳过连续的压缩边界,否则一批连发的边界会各自落到顶层堆叠(pile-up 回归)。
    assert 'orderedMessages[compactionLookahead].kind === "context_compaction_boundary"' in app_js
    assert "const compactionNextReal = orderedMessages[compactionLookahead];" in app_js
    # 折进「已处理」组的登记必须以 compactionIsMidTurn 为前提。
    assert "pendingTurn && compactionIsMidTurn" in app_js


def test_app_js_consecutive_top_level_compaction_boundaries_dedupe() -> None:
    # 多次压缩的摘要标记在 reorder 后可能一起下沉到同一个回合间隙(如低阈值下一回合开头连发多次),
    # turn-ended 顶层分支原本给每条各画一条「上下文已自动压缩」,导致同一处连着出现两条以上纯视觉
    # 噪声的分隔线。去重:紧前一条 orderedMessage 也是压缩边界时跳过绘制,连续段只保留首条一条。
    app_js = _source(APP_JS)
    assert "const consecutiveBoundary =" in app_js
    assert 'prevOrdered.kind === "context_compaction_boundary"' in app_js
    assert "if (!orphanAtTop && !consecutiveBoundary) {" in app_js


def test_styles_has_compaction_boundary_rule() -> None:
    css = _source(STYLES_CSS)
    app_js = _source(APP_JS)
    assert ".context-compaction-boundary" in css
    # Codex 复刻:完成态文案「上下文已自动压缩」+ 图标,左对齐、无横贯分隔线。
    assert "上下文已自动压缩" in app_js
    assert '"context-compaction-boundary-label"' in app_js
    assert 'icon.className = "context-compaction-icon"' in app_js
    # summary 左对齐(flex + align-items: center),不再 text-align: center 居中。
    boundary_summary = css.split(".context-compaction-boundary > summary {", 1)[1].split("}", 1)[0]
    assert "display: flex" in boundary_summary
    assert "text-align: center" not in boundary_summary
    # 完成态不画整行分隔线(去掉 border-top)。
    boundary_block = css.split(".context-compaction-boundary {", 1)[1].split("}", 1)[0]
    assert "border-top" not in boundary_block


def test_session_updated_folds_current_session_into_sidebar_arrays() -> None:
    # LLM 生成/重命名标题经 session.updated 到达时,reducer 只更新 currentSession(主区标题),
    # 侧栏行读 state.sessions[i].title。若不把 currentSession 折进侧栏各数组,侧栏要等 ~2.5s
    # 后台轮询才追平,表现为「主区标题已变、侧栏行仍旧」。handleStreamEvent 收到 session.updated/
    # session.started 时必须调用 replaceUpdatedSessionInState,让侧栏与主区同帧刷新。
    app_source = _source(APP_JS)
    guard = 'event.type === "session.updated" || event.type === "session.started"'
    assert guard in app_source
    fold_call = "replaceUpdatedSessionInState(state, state.currentSession)"
    assert fold_call in app_source
    # 折叠必须发生在 handleStreamEvent 的 reduceAndDedupe 之后(拿到已合并的 currentSession)。
    handler = app_source.split("state = reduceAndDedupe(state, event);", 1)[1]
    assert guard in handler
    assert fold_call in handler


def test_index_html_cache_version_bumped() -> None:
    html = _source(INDEX_HTML)
    assert "web-repl-ui-321" in html
    assert "web-repl-ui-319" not in html


def test_load_sessions_preserves_expanded_project_groups() -> None:
    # 定期后台刷新(perProjectLimit=5)重建 projectGroups 时,必须保留已展开项目的完整会话列表,
    # 否则展开的会话组会在无操作 12s 后被打回 5 条自动收起。loadSessions 须经 preserve 助手过滤。
    app_source = _source(APP_JS)
    assert "function preserveExpandedProjectGroups(freshGroups)" in app_source
    assert "projectGroups: preserveExpandedProjectGroups(payload.projects || [])" in app_source
    # 助手仅在存在展开项目时介入,且对已展开的组用「上一份更长的会话列表」覆盖精简数据。
    assert "if (expandedProjectKeys.size === 0)" in app_source
    assert "if (previousSessions.length <= freshSessions.length)" in app_source
    # 关键:后端 projects 载荷只带 cwd、无 key 字段,而 expandedProjectKeys 存的是
    # groupSessionsByProject 归一化后的 key(projectKeyFromGroup)。保留逻辑必须用同一个
    # projectKeyFromGroup 派生 key,否则 has(group.key)=has(undefined) 恒 false,展开态照样丢。
    preserve_body = app_source.split("function preserveExpandedProjectGroups(freshGroups)", 1)[1].split(
        "async function loadSessions()", 1
    )[0]
    assert "const key = projectKeyFromGroup(group)" in preserve_body
    assert "expandedProjectKeys.has(key)" in preserve_body
    assert "previousByKey.get(key)" in preserve_body
    # 回归护栏:不得退回按原始 group.key 字段取键(后端载荷无 key → undefined → 恒不命中)。
    assert "expandedProjectKeys.has(group.key)" not in preserve_body
    assert "previousByKey.get(group.key)" not in preserve_body
    # 直接整体覆盖 projectGroups 的旧写法(丢失展开状态)不得再出现。
    assert "projectGroups: payload.projects || []," not in app_source


def test_update_banner_periodic_polling_present() -> None:
    # web 是长驻进程:前端须周期轮询 /api/update/status,让运行中发布的新版自动弹出横幅。
    app_source = _source(APP_JS)
    assert "UPDATE_CHECK_INTERVAL_MS" in app_source
    assert "startUpdateAutoCheck" in app_source
    assert "scheduleUpdateCheck" in app_source
    assert "runUpdateCheckTick" in app_source
    # 横幅渲染须幂等(已在则不重复插入)且支持会话级消抹(✕ 后同版本不再重弹)。
    assert "checkForUpdateBanner" in app_source
    assert "updateBannerDismissedVersion" in app_source
    assert "document.querySelector('[data-app-shell=\"update-banner\"]')" in app_source


def test_stack_progress_rendered_on_tool_card() -> None:
    # ros_deploy/ros_stack 的 StackProgressEvent 在 normal 模式挂到工具卡:
    # events.js 据 toolUseId 归并到 tool.stackProgress,tool_cards.js 渲染进度块,
    # styles.css 提供对应样式。三处符号/类名齐备,回归时立即报警。
    events_source = _source(EVENTS_JS)
    tool_cards = _source(TOOL_CARDS_JS)
    styles = _source(STYLES_CSS)

    assert "stackProgress" in events_source
    assert "stack.instances.progress" in events_source
    assert "renderStackProgressDetail" in tool_cards
    assert "tool.stackProgress" in tool_cards
    assert ".tool-stack-progress" in styles
    # 资源/实例进度改用表格(替代旧 <ul>/<li>):列头 + 状态列上色。
    assert "tool-stack-progress-table" in tool_cards
    assert ".tool-stack-progress-table" in styles
    assert "tool-stack-progress-cell-status" in tool_cards
    assert ".tool-stack-progress-cell-status.is-error" in styles
    # 2b 重绘:头部 flex 行 + 进度条(track/fill)+ 状态徽标;标记类齐备。
    assert "tool-stack-progress-bar-fill" in tool_cards
    assert "tool-stack-progress-status" in tool_cards
    assert ".tool-stack-progress-bar-fill" in styles
    assert ".tool-stack-progress-head" in styles


def test_stack_progress_refreshes_outputs_panel() -> None:
    # 资源栈应在部署「开始」即出现,而非完成后:后端 outputs_payload 已能从进行中态派生「创建中」栈,
    # 但仅在拉取 /outputs 时生效。tool.finished 只在终态触发,故 app.js 需在收到 stack.progress
    # (约一个轮询间隔后的首帧)时也去抖刷新输出面板。缺此触发器则栈仍要等 tool.finished 才现身。
    app_source = _source(APP_JS)
    trigger = app_source.split('if (event.type === "pipeline.event") {', 1)
    assert len(trigger) == 2, "缺少 pipeline.event 刷新输出面板的触发块"
    block = trigger[1].split("}", 2)[0] + trigger[1].split("}", 2)[1]
    assert "pipelineEventKind(event.payload)" in block
    assert '"stack.progress"' in block
    assert '"stack.instances.progress"' in block
    assert "scheduleOutputsRefresh()" in block


def test_stack_progress_elapsed_ticks_between_frames() -> None:
    # 问题 2:后端约每十几秒才发一帧,「已用 N 秒」不能只在收帧时跳。events.js 打客户端帧到达时刻
    # receivedAtMs,tool_cards.js 据此在两帧间墙钟插值并写 data-* 基准,app.js 心跳每秒原地续算。
    events_source = _source(EVENTS_JS)
    tool_cards = _source(TOOL_CARDS_JS)
    app_source = _source(APP_JS)

    assert "receivedAtMs: Date.now()" in events_source
    assert "progress.receivedAtMs" in tool_cards
    assert "stackReceivedAt" in tool_cards
    assert "stackElapsedBase" in tool_cards
    assert "function syncStackProgressElapsed" in app_source
    assert 'querySelectorAll(".tool-stack-progress-meta[data-stack-received-at]")' in app_source
    assert "syncStackProgressElapsed(stack)" in app_source


def test_post_handoff_normal_turn_uses_normal_thinking() -> None:
    # 问题 5:交接后 session 仍保留 contextId/taskId,isPipelineTranscript 恒真,但尾部已是普通回合、
    # 没有 working 步骤体 → syncPipelineThinking 找不到落点、无「进行中…」占位。renderMessages 越过
    # 「↪ 普通对话」分隔时置标记,syncTurnThinking 据此改走 syncNormalThinking(底部单枚占位)。
    app_source = _source(APP_JS)
    assert "lastRenderPostHandoffNormal" in app_source
    assert "isPipelineTranscript(state) && !lastRenderPostHandoffNormal" in app_source
    assert "sawNormalBoundary = true;" in app_source


def test_styles_has_step4_diagram_button_rules() -> None:
    css = _source(STYLES_CSS)
    assert ".pipeline-step-diagrams" in css
    # 查看架构图改链接观感、每候选一行、旁加「选择该方案」按钮(两态)。
    assert ".pipeline-step-diagram-item" in css
    assert ".pipeline-step-diagram-link" in css
    assert ".pipeline-step-select-button" in css
    assert ".pipeline-step-select-button.is-confirming" in css
    assert ".pipeline-step-select-button.is-submitting" in css
    # Issue 1:按钮统一右对齐(margin-left:auto)且三态文案下宽度稳定(min-width + 居中)。
    select_block = css.split(".pipeline-step-select-button {", 1)[1].split("}", 1)[0]
    assert "margin-left: auto;" in select_block
    assert "min-width:" in select_block
    assert "justify-content: center;" in select_block
    # 已选方案:候选行绿色对勾样式。
    assert ".pipeline-step-diagram-check" in css
    check_block = css.split(".pipeline-step-diagram-check {", 1)[1].split("}", 1)[0]
    assert "color: #4caf7d;" in check_block
    # 缺图候选的纯文本名与「查看架构图」链接同字号,避免同排两行字号不一致。
    assert ".pipeline-step-diagram-name" in css
    link_block = css.split(".pipeline-step-diagram-link {", 1)[1].split("}", 1)[0]
    name_block = css.split(".pipeline-step-diagram-name {", 1)[1].split("}", 1)[0]
    assert "font-size: 0.86rem;" in link_block
    assert "font-size: 0.86rem;" in name_block


def test_mermaid_render_has_diagram_price():
    js = _source(MERMAID_RENDER_JS)
    assert "export function renderDiagramPrice" in js
    assert "No pricing information" in js
    assert 'ul.className = "pipeline-cost-items"' in js


def test_styles_has_diagram_price_rules():
    css = _source(STYLES_CSS)
    assert ".diagram-price {" in css
    assert ".diagram-price-total" in css
    assert ".diagram-price-empty" in css
    # 询价明细须在深色预览面板上可读:默认 --text-soft(浅底暗字)几乎不可见,
    # 故 .diagram-price 内改用面板自适应的 --codex-text。
    assert ".diagram-price .pipeline-cost-items {" in css
    assert "color: var(--codex-text, var(--text-soft));" in css


def test_stream_render_throttles_while_pointer_over_message_stack() -> None:
    # 指针悬停在转录区时,流式全量重建(replaceChildren)会销毁/重建光标下节点,令 :hover
    # 反复通断("一闪闪")并打断展开点击。scheduleStreamRender 在悬停期间必须把重建合并到
    # 一个低频定时器,指针移开时清掉该定时器并立即追平。
    app_source = _source(APP_JS)
    assert "let messageStackPointerInside = false;" in app_source
    schedule_body = app_source.split("function scheduleStreamRender(", 1)[1].split("\n}\n", 1)[0]
    assert "if (messageStackPointerInside) {" in schedule_body
    assert "HOVER_RENDER_THROTTLE_MS" in schedule_body
    # 进出转录区边界切换悬停标志;pointerleave 清定时器并追平一帧。
    assert 'stack.addEventListener("pointerenter"' in app_source
    assert 'stack.addEventListener("pointerleave"' in app_source
    leave_body = app_source.split('stack.addEventListener("pointerleave"', 1)[1].split("});", 1)[0]
    assert "messageStackPointerInside = false;" in leave_body
    assert "clearHoverThrottle();" in leave_body
    assert "scheduleStreamRender();" in leave_body


def test_api_exposes_output_helpers() -> None:
    source = _source(API_JS)
    assert "export function getOutputs" in source or "getOutputs(" in source
    assert "getOutputFile" in source
    assert "/outputs" in source
    assert 'searchParams.set("path"' in source


def test_api_exposes_review_step_prerequisite_helpers() -> None:
    source = _source(API_JS)
    # 只读探测 + 触发安装两个导出;缺任一,workspace 面板的前置依赖提示接线即失效。
    assert "export function getReviewStepPrerequisite()" in source
    assert "/api/settings/pipeline-review-step/prerequisite" in source
    assert "export async function installReviewStepPrerequisite(onEvent)" in source
    assert "/api/settings/pipeline-review-step/install" in source
    # 安装走 NDJSON 逐行流:必须读 body reader 并按换行切分回调,而非一次性 json。
    assert "getReader()" in source
    assert "application/x-ndjson" in source


def test_workspace_wires_review_step_prerequisite_notice() -> None:
    source = _source(WORKSPACE_JS)
    # 提示块与安装按钮的 marker + 探测/安装接线必须齐备。
    assert "review-step-prereq-notice" in source
    assert "review-step-install" in source
    assert "api.getReviewStepPrerequisite()" in source
    assert "api.installReviewStepPrerequisite(" in source
    # 进度条填充节点(下载百分比 / 不定进度)。
    assert "prereq-progress-fill" in source
    # 三态渲染:已安装/缺失两种状态类都要切换,已安装态必须体现出来(而非隐藏)。
    assert 'classList.toggle("is-installed"' in source
    assert 'classList.toggle("is-missing"' in source
    assert "infraguard is installed; the review step is available." in source


def test_styles_define_review_step_prerequisite_progress() -> None:
    css = _source(STYLES_CSS)
    # [hidden] 必须显式覆盖 display,否则 JS 的 el.hidden=true 在设了 display 的类上失效。
    assert ".review-step-prereq-notice[hidden]" in css
    assert ".prereq-progress-fill" in css
    assert ".prereq-progress-fill.is-indeterminate" in css
    # 减少动效降级:关掉滑动动画。
    assert "prefers-reduced-motion" in css
    # 文案/进度必须走主题变量(而非写死深色),否则暗色主题下看不清。
    assert "color: var(--codex-text)" in css
    assert "color: var(--codex-muted)" in css
    # 已安装态对勾 + 缺失态强调描边 + 按钮 [hidden] 兜底。
    assert ".review-step-prereq-notice.is-installed" in css
    assert ".review-step-prereq-notice.is-missing" in css
    assert ".review-step-prereq-notice .workspace-action[hidden]" in css


def test_app_uses_bumped_api_version_for_outputs() -> None:
    source = _source(APP_JS)
    assert "./api.js?v=web-repl-ui-307" in source
    assert "./api.js?v=web-repl-ui-159" not in source


def test_index_has_output_shell_markup() -> None:
    html = _source(INDEX_HTML)
    assert 'data-app-shell="output-toggle"' in html
    assert 'data-app-shell="output-count"' in html
    assert 'data-app-shell="output-panel"' in html
    assert 'data-app-shell="output-file-preview"' in html


def test_output_panel_module_exists_and_wired() -> None:
    source = _source(OUTPUT_PANEL_JS)
    assert "export function createOutputController" in source
    assert "autoOpenedOnce" in source
    assert "getOutputs" in source
    app_source = _source(APP_JS)
    assert "createOutputController" in app_source
    assert "output_panel.js?v=output-panel-v17" in app_source


def test_output_panel_resets_on_new_session_draft() -> None:
    # 切到新会话草稿(以及草稿落地为真实会话)时须复位输出面板,否则上个会话的
    # 资源栈/模板残留在抽屉与角标里。loadSession 早已复位,这两条路径此前遗漏。
    app_source = _source(APP_JS)
    draft_body = app_source.split("function startNewSessionDraft(", 1)[1].split("\n}", 1)[0]
    assert "outputController?.reset();" in draft_body
    submit_body = app_source.split("async function createSessionForSubmit(", 1)[1].split("\n}", 1)[0]
    assert "outputController?.reset();" in submit_body


def test_output_panel_reset_only_on_session_switch_in_load_session() -> None:
    # loadSession 会在真正切换会话时复位输出面板，但同会话的 resync/重载（流水线运行中权限确认、
    # input_required 等反复触发）绝不能复位：reset() 会强制关闭再由 refresh 自动展开 → 面板「一闪一闪」，
    # 并把用户手动 X 关掉的面板重新弹开。故 loadSession 里的 reset() 必须被 switchedSession 守卫，
    # 而 refresh() 每次都执行（切换与同会话都要拉取最新产物）。
    app_source = _source(APP_JS)
    load_body = app_source.split("async function loadSession(", 1)[1].split("\nasync function ", 1)[0]
    # 引入一次性判定，且 clearDetailsOpenOverrides 与 reset 共用它。
    assert "const switchedSession = previousSessionId && previousSessionId !== state.currentSessionId;" in load_body
    reset_idx = load_body.index("outputController?.reset();")
    guard_idx = load_body.rindex("if (switchedSession) {", 0, reset_idx)
    # reset 之前最近的 if 必须是 switchedSession 守卫（同会话 resync 不复位）。
    assert 0 <= guard_idx < reset_idx
    # refresh 不受守卫限制：紧随其后无条件执行。
    assert "outputController?.refresh(sessionId);" in load_body
    refresh_idx = load_body.index("outputController?.refresh(sessionId);")
    assert refresh_idx > reset_idx


def test_output_panel_hidden_css_guard() -> None:
    css = _source(STYLES_CSS)
    assert ".output-panel[hidden]" in css
    assert ".output-file-preview[hidden]" in css


def test_output_panel_type_icons_wired() -> None:
    # 每行带类型图标:资源栈=堆叠层、模板=文档;开关按钮带面板图标。
    source = _source(OUTPUT_PANEL_JS)
    assert "ROW_ICONS" in source
    assert 'rowIcon("stack")' in source
    assert 'rowIcon("file")' in source
    css = _source(STYLES_CSS)
    assert ".output-row-icon" in css
    assert ".output-toggle-icon" in css
    assert ".output-panel-title" in css


def test_output_preview_and_highlight() -> None:
    source = _source(OUTPUT_PANEL_JS)
    assert "function highlightTemplate" in source
    assert "openPreview" in source
    assert "getOutputFile" in source
    assert "File no longer exists" in source
    assert "tok-" in source
    app_source = _source(APP_JS)
    assert "output_panel.js?v=output-panel-v17" in app_source
    assert "output_panel.js?v=output-panel-v16" not in app_source


def test_output_preview_tok_css() -> None:
    css = _source(STYLES_CSS)
    assert ".tok-key" in css
    assert ".tok-string" in css


def test_output_panel_uses_codex_dark_theme() -> None:
    # 输出面板/预览必须挂到 app 真实的 --codex-* 深色主题;旧实现用了未定义的
    # --panel-bg/--row-bg/--muted-bg(回退浅色默认)→ 白底刺眼、与全站深色不一致。
    # 该断言守住「不得回退到白底」这条硬约束。
    css = _source(STYLES_CSS)
    panel = css.split(".output-panel {", 1)[1].split("}", 1)[0]
    assert "--codex-panel" in panel
    preview = css.split(".output-file-preview {", 1)[1].split("}", 1)[0]
    assert "--codex-panel" in preview
    assert "color:" in preview
    # 已被彻底移除的未定义浅色变量,不得再出现在任何输出面板规则里。
    assert "var(--panel-bg" not in css
    assert "var(--row-bg" not in css
    assert "var(--muted-bg" not in css
    # 深色语法配色(替换掉浅底的 #0550ae/#0a7d33 等)。
    assert "#79c0ff" in css
    assert ".output-preview-fallback" in css


def test_output_panel_renders_architecture_diagram_section() -> None:
    js = _source(OUTPUT_PANEL_JS)
    assert 'from "../mermaid_render.js?v=arch-diagram-v5"' in js
    assert "renderDiagramRow" in js
    assert '"Architecture diagram"' in js
    # 计数把 diagrams 计入(驱动徽标/自动显隐/自动弹出)
    assert "diagrams" in js
    # 预览用 mermaid 渲染而非纯文本
    assert "renderMermaid" in js


def test_output_highlight_escapes_hostile_input(tmp_path: Path) -> None:
    # highlightTemplate 先整体 escapeHtml 再按 token 包裹:模板内容里的恶意标签
    # 必须被转义,不能原样透出为活动 DOM,否则文件预览成为存储型 XSS 注入点。
    source = """
    import { highlightTemplate } from __OUTPUT_PANEL_MODULE__;
    const hostile = '{"x": "</span><img src=x onerror=alert(1)><script>alert(2)</script>"}';
    const outJson = highlightTemplate(hostile, "json");
    const outYaml = highlightTemplate("k: </span><img src=x onerror=alert(1)>", "yaml");
    console.log(JSON.stringify({
      jsonRawImg: outJson.includes("<img"),
      jsonRawScript: outJson.includes("<script>"),
      jsonEscapedLt: outJson.includes("&lt;img"),
      yamlRawImg: outYaml.includes("<img"),
      yamlEscapedLt: outYaml.includes("&lt;img"),
    }));
    """
    result = _run_output_panel_script(tmp_path, source)
    assert result["jsonRawImg"] is False
    assert result["jsonRawScript"] is False
    assert result["jsonEscapedLt"] is True
    assert result["yamlRawImg"] is False
    assert result["yamlEscapedLt"] is True


def test_composer_supports_input_history_navigation() -> None:
    source = _source(COMPOSER_JS)
    # 两级历史 + localStorage 全局键
    assert "iac-code:input-history:global" in source
    assert "conversationHistory" in source
    assert "globalHistory" in source
    # 公开 API 与捕获
    assert "setInputHistory(items)" in source
    assert "function rememberInput(raw)" in source
    # 导航:未导航时非空放行默认;导航中编辑过则退出放行默认
    assert "function handleHistoryNavigation(key)" in source
    assert "conversationHistory.length ? conversationHistory : globalHistory" in source
    assert "return false; // 输入框非空:放行默认(多行光标移动)" in source
    # keydown 合并分支只在联想或历史导航时拦截
    assert 'if (event.key === "ArrowDown" || event.key === "ArrowUp") {' in source
    assert "if (handleHistoryNavigation(event.key)) {" in source


def test_mermaid_render_helper_lazy_loads_and_falls_back() -> None:
    js = _source(MERMAID_RENDER_JS)
    assert "export async function renderMermaid" in js
    # 懒加载:动态注入 vendor 脚本,而非 index.html 启动加载
    assert 'document.createElement("script")' in js
    assert "/static/js/vendor/mermaid.min.js?v=10.9.3" in js
    assert 'securityLevel: "strict"' in js
    # 失败回退原文
    assert "mermaid-fallback" in js


def test_mermaid_vendor_bundle_present() -> None:
    assert MERMAID_VENDOR_JS.exists() and MERMAID_VENDOR_JS.stat().st_size > 0


def test_pipeline_candidate_inline_diagram_uses_web_diagrams() -> None:
    js = _source(PIPELINE_JS)
    assert 'from "../mermaid_render.js?v=arch-diagram-v5"' in js
    assert "webDiagrams" in js  # combinedDiagrams 合并 state.webDiagrams
    assert "pipeline-candidate-diagram" in js  # 每卡可折叠架构图
    assert "renderMermaid" in js
    # 候选↔图匹配须 index 优先(重名候选靠 candidate_index 区分),name 仅兜底。
    assert "duplicate-name discriminator" in js


def test_pipeline_js_import_is_versioned() -> None:
    # pipeline.js 之前是 app.js 里唯一无版本位的 import;内容改动(含本轮 index 优先
    # 匹配修复)在回访浏览器的 warm cache 下不会重新拉取。加版本位以确保修复落地。
    app_source = _source(APP_JS)
    assert "./components/pipeline.js?v=pipeline-arch-v7" in app_source


def test_app_stores_web_diagrams_from_outputs() -> None:
    js = _source(APP_JS)
    # 断言真实赋值线,而非 "webDiagrams" 是否出现(后者连注释都能命中)。
    assert "state.webDiagrams = payload.diagrams" in js


def test_app_stores_web_candidates_from_outputs() -> None:
    js = _source(APP_JS)
    # 权威候选表落到 state.webCandidates,供 confirm_and_select 选择器渲染全部候选。
    assert "state.webCandidates = payload.candidates" in js


def test_output_panel_refresh_forwards_candidates_to_onpayload(tmp_path: Path) -> None:
    # 根因回归:refresh() 重塑 payload 时曾只透传 stacks/files/diagrams,把 candidates 丢弃,
    # 于是 onPayload 收到的候选表恒为空,confirm_and_select 选择器退回「按可渲染架构图」渲染——
    # 某候选模板 YAML 损坏无图时就少一行(出 2 个方案只显示 1 个)。此测试驱动真实 refresh(),
    # 用返回 2 个候选、1 张架构图的 stub api,断言 onPayload 拿到的 payload 携带全部 candidates。
    source = """
    // createOutputController 自持 DOM(byShell → document.querySelector);node 无 DOM,
    // 用万能 no-op 代理桩接管 document,让控制器创建/渲染路径不因缺元素报错。
    const stubEl = new Proxy(
      {},
      {
        get(target, prop) {
          if (prop in target) return target[prop];
          return () => {};
        },
        set(target, prop, value) {
          target[prop] = value;
          return true;
        },
      },
    );
    globalThis.document = { querySelector: () => stubEl, createElement: () => stubEl };

    const { createOutputController } = await import(__OUTPUT_PANEL_MODULE__);

    const outputsResponse = {
      stacks: [],
      files: [{ path: "/x/1.yml", name: "1.yml", format: "yaml", relPath: "1.yml" }],
      diagrams: [
        { diagramId: "1:/x/b.yml", candidateName: "balanced", candidateIndex: 1 },
      ],
      candidates: [
        { candidateName: "经济极简方案", candidateIndex: 0, summary: "s0" },
        { candidateName: "均衡性价比方案", candidateIndex: 1, summary: "s1" },
      ],
    };

    let received = null;
    const controller = createOutputController({
      getSessionId: () => "s1",
      api: { getOutputs: async () => outputsResponse },
      onPayload: (payload) => {
        received = payload;
      },
    });

    await controller.refresh("s1");

    console.log(
      JSON.stringify({
        onPayloadCalled: received !== null,
        candidateCount: received && Array.isArray(received.candidates) ? received.candidates.length : -1,
        candidateIndexes:
          received && Array.isArray(received.candidates) ? received.candidates.map((c) => c.candidateIndex) : [],
        diagramCount: received && Array.isArray(received.diagrams) ? received.diagrams.length : -1,
      }),
    );
    """
    result = _run_output_panel_script(tmp_path, source)
    assert result["onPayloadCalled"] is True
    assert result["candidateCount"] == 2
    assert result["candidateIndexes"] == [0, 1]
    # diagrams 仍原样透传(只有可渲染的 idx1),与 candidates 全量形成对照。
    assert result["diagramCount"] == 1


def test_output_panel_diagram_preview_guard_uses_diagram_id() -> None:
    # 预览陈旧性守卫用唯一 diagramId 作键(重名候选 title 可能相同,无法区分)。
    source = _source(OUTPUT_PANEL_JS)
    assert "item.diagramId || title" in source


def test_output_controller_exposes_open_diagram_preview() -> None:
    js = _source(OUTPUT_PANEL_JS)
    # createOutputController 返回对象须暴露 openDiagramPreview 与 toggleDiagramPreview,
    # 供转录 step4 链接调用(前者打开、后者开/关切换)。
    assert "openDiagramPreview," in js
    assert "toggleDiagramPreview," in js
    assert "return { refresh, reset, openDiagramPreview, toggleDiagramPreview" in js
    # 切换预览:同一图已可见则关闭,否则打开。
    assert "function toggleDiagramPreview(item)" in js
    assert "activePreviewPath === key && preview && !preview.hidden" in js


def test_app_renders_step4_diagram_link_and_select() -> None:
    js = _source(APP_JS)
    # step4 守卫 + 链接/选择按钮类名 + 文案 + 调用点注入(链接切换、选择两击确认)。
    assert 'stepId === "confirm_and_select"' in js
    assert "pipeline-step-diagram-link" in js
    assert "pipeline-step-select-button" in js
    assert 't("View diagram")' in js
    assert 't("Select this option")' in js
    assert 't("Confirm selection?")' in js
    assert 't("Selecting…")' in js
    # 链接切换:调用点注入 toggleDiagramPreview;选择:注入 handleSelectPipelineCandidate。
    assert "toggleDiagram: (item) => outputController?.toggleDiagramPreview?.(item)" in js
    assert "handleSelectPipelineCandidate({ candidateName: item.candidateName," in js
    assert "candidateIndex: item.candidateIndex })" in js
    assert "diagrams: overlayDiagramOptimization(state.webDiagrams || [], state)" in js
    # 已选方案:该候选行绿色对勾;选中判定复用 resolvePipelineSelectedCandidate(与工作台弹窗同源)。
    assert "pipeline-step-diagram-check" in js
    assert "isSelectedDiagramCandidate" in js
    assert "selectedCandidate: resolvePipelineSelectedCandidate(state)" in js
    # 候选选择器只列候选图:据 candidateIndex 过滤,挡掉部署步骤按路径写出的最终模板(重复按钮)。
    assert "const candidateDiagrams = diagrams.filter(" in js
    assert "item.candidateIndex !== null && item.candidateIndex !== undefined" in js
    # 根因修复:按权威候选表(input_required.options)渲染,架构图按 candidateIndex 合并;
    # 候选表为空时回退到「按可渲染架构图」旧逻辑。缺图候选仍成行、可选(纯文本名标签占位)。
    assert "const candidates = Array.isArray(options.candidates) ? options.candidates : [];" in js
    assert "candidateRows" in js
    assert 'if (stepId === "confirm_and_select" && candidateRows.length)' in js
    assert "for (const item of candidateRows)" in js
    assert "candidates: state.webCandidates || []" in js
    assert "pipeline-step-diagram-name" in js
    # 两击确认的模块级武装态 + 外部点击复位(仿 workspace 一次性监听)。
    assert "let armedSelectButton = null;" in js
    assert "function disarmSelectButton()" in js
    # Issue 2:全局提交锁——任一候选提交中,所有按钮拒绝点击;失败复位并解锁。
    assert "let selectSubmitting = false;" in js
    assert "if (selectSubmitting) {" in js
    assert "selectSubmitting = true;" in js
    assert "selectSubmitting = false;" in js


def test_app_regroups_pipeline_messages_before_render() -> None:
    js = _source(APP_JS)
    # Issue 3:渲染前按父子关系把并行候选子树重排成连续段,并在渲染循环里消费重排后的数组。
    assert "export function regroupPipelineMessages(messages)" in js
    assert "const orderedMessages = regroupPipelineMessages(messages);" in js
    # 索引式循环:压缩边界分支需向后跳过连续边界(compactionLookahead)判定是否回合中途。
    assert "for (let messageIndex = 0; messageIndex < orderedMessages.length; messageIndex += 1) {" in js
    assert "const message = orderedMessages[messageIndex];" in js


def test_app_output_panel_import_bumped_to_v11() -> None:
    js = _source(APP_JS)
    assert "output-panel-v17" in js
    assert "output-panel-v16" not in js


def test_appearance_theme_css_blocks_present() -> None:
    styles = _source(STYLES_CSS)
    # 默认块补入的基色 token（ink 供叠加层翻转，rail-top/bottom 供渐变 token 化）
    assert "--codex-ink: #ffffff;" in styles
    assert "--codex-rail-top: #2e2e2e;" in styles
    assert "--codex-rail-bottom: #252525;" in styles
    # 4 段非默认主题块(graphite=默认,由裸 :root 覆盖,不单独出块)
    for slug in ("midnight", "evergreen", "sepia", "ivory"):
        assert '[data-theme="{}"]'.format(slug) in styles
    # graphite 走默认 :root,不应出现独立主题块(独立块会与默认块逐字重复)
    assert '[data-theme="graphite"]' not in styles
    # 主题特征值抽查
    assert "--codex-unread: #5b9cff;" in styles  # midnight
    assert "--codex-unread: #2f6fed;" in styles  # ivory
    assert "color-scheme: light;" in styles  # ivory
    assert "--codex-ink: #1c1c1a;" in styles  # ivory 近黑墨
    # 渐变已 token 化（不再硬编码 #2e2e2e / #252525 端点）
    gradient = "linear-gradient(180deg, var(--codex-rail-top) 0%, var(--codex-rail) 46%, var(--codex-rail-bottom) 100%)"
    assert gradient in styles


def test_appearance_theme_swatch_styles_present() -> None:
    styles = _source(STYLES_CSS)
    assert ".workspace-theme-grid" in styles
    assert ".workspace-theme-swatch" in styles
    assert ".workspace-theme-swatch.is-active" in styles


def test_appearance_frontend_wiring() -> None:
    api_source = _source(API_JS)
    workspace_source = _source(WORKSPACE_JS)
    assert "export function getAppearance()" in api_source
    assert "export function saveAppearance(" in api_source
    assert "/api/settings/appearance" in api_source
    assert "Color scheme" in workspace_source
    assert "workspace-theme-swatch" in workspace_source
    assert "dataset.theme" in workspace_source
    assert "getAppearance" in workspace_source
    assert "saveAppearance" in workspace_source


def test_ivory_syntax_highlight_variant_present() -> None:
    styles = _source(STYLES_CSS)
    # ivory 作用域下的浅色语法配色(GitHub-light 系)
    assert ':root[data-theme="ivory"] .output-file-preview .tok-key' in styles


def test_no_unconverted_white_overlays_remain() -> None:
    """守护:styles.css 中 rgba(255, 255, 255, α) 白字面量出现次数锁定。
    转换后允许保留的类别:
      - 主题 token 定义
      - var(--codex-X, rgba(255,255,255,α)) 回退形式(约 27 处)
      - 基础对话渐变、遮罩层
      - composer focus 白环(ivory 由 focus-within 覆盖整条阴影)
      - 合法深色岛上的白字/白环(tooltip / suggestion / result / diagram)
    Task 10 转换了 27 处覆盖层白字面量(10 原始残留 + 17 内部覆盖层)。
    出现次数上升 = 新引入了未转换的白覆盖层,应改为 color-mix ink 转换。
    """
    source = _source(STYLES_CSS)
    EXPECTED_WHITE_LITERALS = 47  # noqa: N806
    assert source.count("rgba(255, 255, 255,") == EXPECTED_WHITE_LITERALS


def test_ivory_accent_text_overrides_present() -> None:
    """守护:ivory 下硬编码浅色强调文字/accent 回退已加深色可读覆盖。"""
    source = _source(STYLES_CSS)
    for needle in (
        ':root[data-theme="ivory"] .app-modal-error { color: #c0392b; }',
        ':root[data-theme="ivory"] .workspace-skill-source-bundled { color: #0969da; }',
        ':root[data-theme="ivory"] .workspace-skill-source-project { color: #1a7f37; }',
        ':root[data-theme="ivory"] .workspace-skill-source-user { color: #9a6700; }',
        ':root[data-theme="ivory"] .permission-mode-control.is-accept-edits { color: #0969da; }',
        ':root[data-theme="ivory"] .permission-mode-control.is-bypass-permissions { color: #bc4c00; }',
    ):
        assert needle in source
    assert ':root[data-theme="ivory"] .sidebar-resize-handle:hover::after,' in source
    assert "background: color-mix(in srgb, var(--codex-ink) 28%, transparent);" in source


def test_ivory_pipeline_step_icon_override_present() -> None:
    """守护:ivory 下流水线步骤图标(◎/◇)由近白改深墨,浅底可见;深色主题不受影响。"""
    source = _source(STYLES_CSS)
    assert ':root[data-theme="ivory"] .pipeline-step-group > .pipeline-step-summary .pipeline-step-icon,' in source
    assert (
        ':root[data-theme="ivory"] .pipeline-candidate-group > .pipeline-step-summary .pipeline-step-icon {' in source
    )
    assert "border-color: color-mix(in srgb, var(--codex-ink) 30%, transparent);" in source
    assert "color: color-mix(in srgb, var(--codex-ink) 70%, transparent);" in source


def test_ivory_primary_button_hover_stays_dark() -> None:
    """守护:ivory 下设置界面主行动按钮(深底浅字)的 :hover 不再刷成纯白底。

    深色主题的基础 :hover 把底色变白是为浅底按钮设计的;象牙浅底下这些按钮是深底浅字,
    悬停变白底后浅字消失(用户报告「黑色按钮鼠标移上去看不见」)。此处悬停仅轻微提亮深底。
    """
    source = _source(STYLES_CSS)
    for needle in (
        ':root[data-theme="ivory"] .workspace-mcp-card .workspace-action-primary:hover:not(:disabled),',
        ':root[data-theme="ivory"] .workspace-settings-provider .workspace-action-primary:hover:not(:disabled),',
        ':root[data-theme="ivory"] .workspace-settings-group-cloud[open]'
        " .workspace-action-primary:hover:not(:disabled),",
        ':root[data-theme="ivory"] .workspace-memory-note-foot .workspace-action-primary:hover:not(:disabled),',
        ':root[data-theme="ivory"] .workspace-provider-form-footer .workspace-action-primary:hover:not(:disabled) {',
    ):
        assert needle in source
    assert "background: color-mix(in srgb, var(--codex-ink) 82%, #ffffff);" in source


def test_appearance_saved_status_auto_clears() -> None:
    """守护:配色/可见性设置的「已保存」提示为瞬时反馈,持久化成功后自动清除,不永久驻留。"""
    source = _source(WORKSPACE_JS)
    assert "const stampSaved = (token) =>" in source
    assert "clearTimer = setTimeout(" in source
    # persist 与 selectTheme 都改用自动清除的成功提示 stampSaved(token)(两处调用)。
    assert source.count("stampSaved(token)") >= 2


def _run_mermaid_render_script(tmp_path, source):
    harness = (
        "const nodes = [];\n"
        "function mk(tag){const n={tagName:tag,className:'',children:[],_text:'',"
        "set textContent(v){this._text=String(v);},"
        "get textContent(){return this._text + this.children.map(c=>c.textContent).join('');},"
        "append(...cs){this.children.push(...cs);},"
        "appendChild(c){this.children.push(c);return c;}};nodes.push(n);return n;}\n"
        "globalThis.document={createElement:mk};\n"
        + source.replace("__MERMAID_MODULE__", MERMAID_RENDER_JS.resolve().as_uri())
    )
    script = tmp_path / "probe.mjs"
    script.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_render_diagram_price_with_cost(tmp_path):
    out = _run_mermaid_render_script(
        tmp_path,
        "const { renderDiagramPrice } = await import('__MERMAID_MODULE__');\n"
        "const el = renderDiagramPrice({ totalMonthlyCost: '¥120/月',"
        " costItems: [{ name: 'ECS', spec: '2c4g', monthly_cost: '¥100' },"
        " { name: 'RDS', monthly_cost: '¥20' }] });\n"
        "function findClass(n, cls){ if((n.className||'')===cls) return n;"
        " for(const c of (n.children||[])){ const r=findClass(c,cls); if(r) return r; } return null; }\n"
        "function countLi(n, acc){ acc=acc||{v:0}; if(n.tagName==='li') acc.v++;"
        " for(const c of (n.children||[])) countLi(c, acc); return acc.v; }\n"
        "const total = findClass(el, 'diagram-price-total');\n"
        "const ul = findClass(el, 'pipeline-cost-items');\n"
        "console.log(JSON.stringify({ hasTotal: !!total, totalText: total? total.textContent : '',"
        " liCount: ul? countLi(ul,{v:0}) : 0,"
        " empty: !!findClass(el, 'diagram-price-empty') }));\n",
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert data["hasTotal"] is True
    assert "120" in data["totalText"]
    assert data["liCount"] == 2
    assert data["empty"] is False


def test_render_diagram_price_without_cost(tmp_path):
    out = _run_mermaid_render_script(
        tmp_path,
        "const { renderDiagramPrice } = await import('__MERMAID_MODULE__');\n"
        "const el = renderDiagramPrice({});\n"
        "function findClass(n, cls){ if((n.className||'')===cls) return n;"
        " for(const c of (n.children||[])){ const r=findClass(c,cls); if(r) return r; } return null; }\n"
        "const empty = findClass(el, 'diagram-price-empty');\n"
        "console.log(JSON.stringify({ empty: !!empty, emptyText: empty? empty.textContent : '',"
        " hasUl: !!findClass(el, 'pipeline-cost-items') }));\n",
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert data["empty"] is True
    assert "No pricing information" in data["emptyText"]
    assert data["hasUl"] is False


def test_diagram_price_wired_into_both_render_sites():
    op = _source(OUTPUT_PANEL_JS)
    pl = _source(PIPELINE_JS)
    assert "renderDiagramPrice" in op
    assert "container.append(renderDiagramPrice(item))" in op
    assert "renderDiagramPrice" in pl
    assert "body.append(renderDiagramPrice(match))" in pl


def test_thinking_toggle_button_in_composer_toolbar():
    # 思考开关是 composer 工具栏(权限右侧)的切换按钮,不再是斜杠菜单项。
    html = _source(INDEX_HTML)
    assert 'data-app-shell="thinking-toggle"' in html
    assert 'class="thinking-toggle"' in html
    assert 'aria-pressed="false"' in html
    # 位置:排在权限选择器之后、模型选择器之前。
    permission_idx = html.index("permission-mode-picker")
    thinking_idx = html.index('data-app-shell="thinking-toggle"')
    model_idx = html.index("composer-model-picker")
    assert permission_idx < thinking_idx < model_idx


def test_thinking_toggle_styles_present():
    css = _source(STYLES_CSS)
    block = _css_block(css, ".thinking-toggle")
    assert "border-radius: 999px" in block
    # 开启态用高亮色,与权限「接受编辑」一致的蓝色。
    on_block = _css_block(css, ".thinking-toggle.is-on")
    assert "#7cc2ff" in on_block


def test_composer_wires_thinking_toggle():
    app = _source(APP_JS)
    composer = _source(COMPOSER_JS)
    api = _source(API_JS)
    # app.js 采集按钮元素并注入 composer。
    assert 'byShell("thinking-toggle")' in app
    assert "onThinkingEnabledChange" in app
    # 流水线会话隐藏 /compact:app.js 依据 currentSession.mode / 流水线草稿 提供 isPipelineMode 回调。
    assert "isPipelineMode: () =>" in app
    assert 'text(state.currentSession?.mode) === "pipeline"' in app
    # false 是有效值(显式关),draft 站点须用 ?? 而非 || 以免被回退覆盖。
    # 初始渲染取 thinkingEffective(override 优先,否则 provider 默认),避免旧会话 override=null 一律显示为“关”。
    assert "draft?.thinkingEnabled ?? session.thinkingEffective" in app
    # composer 控制器渲染/保存/暴露 setter。
    assert "function renderThinkingToggle" in composer
    assert "async function saveThinkingEnabled" in composer
    assert "setThinkingEnabled(value)" in composer
    # 新会话草稿:setThinkingEnabled 收到非布尔值时跟随所选模型默认,provider 解析后重算。
    assert "let thinkingFollowsDefault = false;" in composer
    assert "function selectedModelThinkingDefault()" in composer
    assert "selectedModel()?.thinkingDefault === true" in composer
    assert "if (thinkingFollowsDefault) {" in composer
    # api.js 提供会话级持久化端点封装。
    assert "export function saveThinkingEnabled" in api
    assert '"/thinking-enabled"' in api


def test_app_reads_injected_session_defaults():
    app = _source(APP_JS)
    # 后端把新会话默认注入 <body> data 属性,app.js 读一次做模块级常量,避免异步拉取闪烁。
    assert "const SESSION_DEFAULTS = readInjectedSessionDefaults();" in app
    assert "function readInjectedSessionDefaults()" in app
    assert "dataset.defaultPermissionMode" in app
    assert "dataset.defaultMode" in app
    assert "dataset.defaultPipelineName" in app
    # 草稿构造优先级:显式入参 > 上一草稿 > 注入的用户默认。
    assert "SESSION_DEFAULTS.permissionMode" in app
    assert "SESSION_DEFAULTS.mode" in app
    assert "SESSION_DEFAULTS.pipelineName" in app


def test_api_exposes_session_defaults_endpoints():
    api = _source(API_JS)
    assert "export function getSessionDefaults" in api
    assert "export function saveSessionDefaults" in api
    assert '"/api/settings/session-defaults"' in api


def test_workspace_other_panel_has_session_defaults_group():
    source = _source(WORKSPACE_JS)
    # 「新会话默认」分组:权限下拉 + 模式二级弹出选择器 + 保存钩子 + 加载。
    assert "New session defaults" in source
    assert "SESSION_DEFAULT_PERMISSION_OPTIONS" in source
    assert 'makeSelect("session-default-permission")' in source
    assert "async function persistSessionDefaults()" in source
    assert "api.saveSessionDefaults(" in source
    assert "api.getSessionDefaults()" in source
    # 权限下拉 change 即写回;模式选择器由弹出项点击直接 persist。
    assert 'permissionSelect.addEventListener("change", persistSessionDefaults)' in source
    # 默认模式复刻 composer 的二级弹出(普通模式 / 流水线模式 → 子菜单),复用 draft-session-* 类名。
    assert 'dataset: { workspaceAction: "session-default-mode" }' in source
    assert '"draft-session-picker draft-mode-picker workspace-mode-picker"' in source
    assert '"draft-session-menu draft-mode-menu"' in source
    assert '"draft-session-menu draft-session-submenu draft-pipeline-submenu"' in source
    assert "const makeModeMenuItem" in source
    assert "Pipeline mode" in source
    assert "is-selling-pipeline" in source
    assert "const normalizeModeSelection" in source
    assert "context.pipelineOptions" in source
    # createOtherPanel 构造期即执行,须守卫 document.addEventListener 存在再挂点击外收起。
    assert 'typeof document.addEventListener === "function"' in source
    # 面板内弹出改向下展开的作用域覆盖。
    assert '"workspace-settings-group workspace-settings-provider workspace-session-defaults-card"' in source
    # card overflow:visible 不足以逃出 .workspace-content 滚动盒,打开时用 position:fixed
    # 按触发器 rect 实算坐标把菜单钉到视口,脱离所有 overflow 祖先;无头桩缺 window/rect 自然跳过。
    assert "const positionModeMenus" in source
    assert "MODE_MENU_GAP_PX" in source
    assert 'modeMenu.style.position = "fixed"' in source
    assert "modeTrigger.getBoundingClientRect()" in source
    assert 'typeof window === "undefined"' in source
    assert "positionModeMenus();" in source
    # 保存新默认后须通知 app 同步内存 + 当前草稿,否则改了默认要刷新页面才生效。
    assert "context.onSessionDefaultsSaved?.(" in source
    # context 必须把 options 回调透传给各面板,否则上面的通知是 undefined?.() 静默丢弃。
    assert "onSessionDefaultsSaved: (payload) => options.onSessionDefaultsSaved" in source
    app_source = _source(APP_JS)
    assert "export function normalizeSessionDefaults" in app_source
    assert "export function applySessionDefaults" in app_source
    assert "Object.assign(SESSION_DEFAULTS" in app_source
    assert "onSessionDefaultsSaved: applySessionDefaults" in app_source
    css = _source(STYLES_CSS)
    assert ".workspace-mode-picker .draft-session-menu" in css
    # 卡片放开 overflow,否则向下弹出的模式菜单被裁在卡片底边(fixed 失效时的 CSS 兜底)。
    assert ".workspace-session-defaults-card" in css
    assert "overflow: visible" in css


def test_app_passes_pipeline_options_to_workspace():
    app = _source(APP_JS)
    # 新会话默认面板的「默认流水线」下拉数据源来自 app.js 的 PIPELINE_OPTIONS。
    assert "pipelineOptions: PIPELINE_OPTIONS" in app


def test_restart_server_wired_across_frontend():
    # api.js 暴露重启调用。
    api_src = _source(API_JS)
    assert "export function restartServer()" in api_src
    assert "/api/server/restart" in api_src

    # workspace.js 常规面板渲染「重启服务」按钮 + 全屏确认遮罩 + 健康轮询后自动刷新。
    ws = _source(WORKSPACE_JS)
    assert '"server-restart"' in ws
    assert "server-restart-overlay" in ws
    assert "api.restartServer()" in ws
    assert '"/health"' in ws
    assert "window.location.reload()" in ws
    # 两阶段轮询:先确认下线(sawDown)再等恢复,避免命中重启前的旧进程 200 而误刷新。
    assert "sawDown" in ws
    # 重启中显示转圈进度,而非静止文案。
    assert "server-restart-spinner" in ws

    # styles.css 提供遮罩与危险按钮样式(遮罩用黑色背板,非被禁的白色 rgba 字面量)。
    css = _source(STYLES_CSS)
    assert ".server-restart-overlay" in css
    assert ".workspace-action.is-danger" in css
    assert ".server-restart-spinner" in css


def test_update_api_functions_exposed() -> None:
    source = _source(API_JS)
    assert "export function getUpdateStatus()" in source
    assert "export function applyUpdate()" in source
    assert "export function dismissUpdate()" in source
    assert "/api/update/status" in source
    assert "/api/update/apply" in source
    assert "/api/update/dismiss" in source


def test_update_banner_wired_across_frontend() -> None:
    app_source = _source(APP_JS)
    # 启动后拉状态并渲染顶部 banner。
    assert "api.getUpdateStatus()" in app_source
    assert "update-banner" in app_source
    # 三个控件:立即更新 / 不再提醒此版本(dismiss)/ 关闭。
    assert "api.applyUpdate()" in app_source
    assert "api.dismissUpdate()" in app_source
    assert "update-banner-close" in app_source
    # 成功后复用重启流程:调 restartServer + 两阶段 /health 轮询后刷新。
    assert "api.restartServer()" in app_source
    assert '"/health"' in app_source
    assert "sawDown" in app_source
    assert "window.location.reload()" in app_source
    # 升级进行中复用现有转圈样式。
    assert "server-restart-spinner" in app_source
    # 失败态给消息加 is-error(红字);手动刷新兜底须清 spinner 而非悬空。
    assert 'msg.classList.add("is-error")' in app_source
    assert 'msg.classList.remove("is-error")' in app_source

    styles = _source(STYLES_CSS)
    assert ".update-banner" in styles
    # banner 用 codex 主题变量(深色友好),不用浅色遗留变量。
    assert "--codex-panel-raised" in styles
    # 失败文案红色:codex danger 变量(带回退),不是白字。
    assert ".update-banner-msg.is-error" in styles


def test_language_selector_wired() -> None:
    api_source = _source(API_JS)
    assert "getUiLanguage" in api_source
    assert "saveUiLanguage" in api_source

    workspace_source = _source(WORKSPACE_JS)
    assert 'import { t, currentLang } from "../i18n.js' in workspace_source
    assert "saveUiLanguage" in workspace_source
    assert "location.reload()" in workspace_source


def test_update_banner_uses_codex_theme_only_no_light_vars() -> None:
    # 定位 .update-banner 相关规则块,确认没引用浅色遗留变量(上次重启面板踩过的坑)。
    styles = _source(STYLES_CSS)
    start = styles.index(".update-banner")
    block = styles[start : start + 1200]
    for legacy in ("var(--panel)", "var(--line)", "var(--text-soft)", "var(--danger)"):
        assert legacy not in block


def test_index_html_uses_data_i18n_markers() -> None:
    html = _source(INDEX_HTML)
    assert 'data-i18n="New chat"' in html
    assert html.count("data-i18n") >= 30


def test_developer_mode_wiring_present() -> None:
    # 开发者模式:常规分页放总开关(workspace-developer-mode),开启后 NAV_GROUPS 里
    # devOnly 的「开发」分页才出现;api.js 暴露读写端点;失败工具标红门控于 body class。
    workspace = _source(WORKSPACE_JS)
    api = _source(API_JS)
    app = _source(APP_JS)

    # 总开关 + devOnly 分页 + 专属面板
    assert 'makeForeignSwitch("workspace-developer-mode")' in workspace
    assert "devOnly: true" in workspace
    assert "function createDeveloperPanel" in workspace
    # 「失败工具标红」开关(功能1)与其 body class 切换
    assert 'makeForeignSwitch("workspace-highlight-failed-tools")' in workspace
    assert 'classList.toggle("dev-highlight-tool-errors"' in workspace

    # api.js 客户端读写端点(功能持久化)
    assert "export function getDeveloperSettings" in api
    assert "export function saveDeveloperSettings" in api
    assert "/api/settings/developer" in api

    # app.js 启动时按已保存设置应用标红 body class
    assert "applyDeveloperHighlightFromSettings" in app
    assert "api.getDeveloperSettings()" in app


def test_failed_tool_highlight_gated_under_body_class() -> None:
    # 功能1:失败工具标红规则必须门控在 body.dev-highlight-tool-errors 下,
    # 开关关闭(默认)时失败工具卡与其它工具一视同仁。
    css = _source(STYLES_CSS)
    for rule in (
        "body.dev-highlight-tool-errors .message-tool-cards .tool-card.is-error",
        "body.dev-highlight-tool-errors .message-tool-cards .tool-stack-progress-status.is-error",
        "body.dev-highlight-tool-errors .message-tool-cards .tool-stack-progress-bar-fill.is-error",
        "body.dev-highlight-tool-errors .message-tool-cards .tool-stack-progress-cell-status.is-error",
    ):
        assert rule in css
