"""Offline contracts for optional ROS sales-page guidance and request-local links."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from iac_code.agent.system_prompt import build_system_prompt
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.selling_solution_first.hooks.solution_planning_and_selection import (
    build_completion_reply,
    build_prompt_context,
)
from iac_code.services.providers.aliyun import DEFAULT_REGION, AliyunCredential, use_aliyun_credential
from iac_code.services.session_storage import SessionStorage
from iac_code.tools.base import ToolRegistry
from iac_code.types.stream_events import (
    MessageEndEvent,
    MessageStartEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    Usage,
)

PIPELINE = Path(__file__).resolve().parents[3] / "src/iac_code/pipeline/selling_solution_first"


@pytest.mark.parametrize("enabled,status,reason,visible,expected", [
    (True, "rejected", "valid", "", True),
    (True, "rejected", "valid", "intro", True),
    (True, "rejected", "valid", "valid", False),
    (True, "rejected", "valid", "markdown", False),
    (False, "rejected", "valid", "", False),
    (True, "awaiting_selection", "valid", "", False),
    (True, "rejected", "cancel", "", False),
    (True, "rejected", "wrong-region", "", False),
    (True, "rejected", "wrong-host", "", False),
    (True, "rejected", "empty-prompt", "", False),
    (True, "rejected", "fragment", "", False),
])
def test_handoff_reply_is_scoped_and_deduplicated(monkeypatch, enabled, status, reason, visible, expected):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", str(enabled).lower())
    link = "https://ros.console.aliyun.com/cn-beijing/chat?prompt=ECS%20billing"
    values = {
        "valid": link, "markdown": f"[AI 助手]({link})", "intro": "我来生成链接。", "": "",
        "cancel": "用户取消部署。", "wrong-region": link.replace("cn-beijing", "cn-shanghai"),
        "wrong-host": link.replace("ros.console.aliyun.com", "example.com"),
        "empty-prompt": link.split("=")[0] + "=", "fragment": link + "#fragment",
    }
    with use_aliyun_credential(AliyunCredential(region_id="cn-beijing")):
        reply = build_completion_reply(
            conclusion={"status": status, "rejection_reason": values[reason]}, visible_text=values[visible],
        )
    assert reply == (values[reason] if expected else None)


@pytest.fixture(autouse=True)
def isolate_cloud_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("iac_code.services.providers.aliyun.DEFAULT_ALIYUN_CLI_CONFIG_PATH", str(tmp_path / "cli.json"))
    for name in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_SECURITY_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def executor(tmp_path):
    pipeline = load_pipeline_dir(PIPELINE)
    return StepExecutor(
        provider_manager=SimpleNamespace(),
        base_tool_registry=ToolRegistry(),
        pipeline=pipeline,
        pipeline_dir=PIPELINE,
        cwd=str(tmp_path),
    )


@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "no", "unexpected"])
def test_disabled_awareness_does_not_add_handoff_or_read_credentials(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("IAC_CODE_ROS_AGENT_AWARENESS", raising=False)
    else:
        monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", value)

    def unexpected_read():
        raise AssertionError("Disabled feature must not read a request credential")

    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials.load", unexpected_read)
    assert build_prompt_context() == {"ros_agent_awareness": ""}


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_enabled_awareness_uses_session_region(monkeypatch, value):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", value)
    with use_aliyun_credential(AliyunCredential(region_id="cn-shanghai")):
        supplement = build_prompt_context()["ros_agent_awareness"]
    assert "[AI 助手](https://ros.console.aliyun.com/cn-shanghai/chat)" in supplement
    assert "{ai_assistant_destination}" not in supplement
    assert len(supplement) <= 800, "Keep the optional guidance short"
    assert "在链接 URL 内添加 `?prompt=`" in supplement
    assert "包含必要上下文、可独立理解的精炼用户问题" in supplement
    assert "URL 编码（含保留字符）" in supplement
    assert "云资源的购买、创建、部署请求留在当前流程" in supplement
    assert "资源清单、架构图等云方案的分析请求若部署意愿不明" in supplement
    assert "`ask_user_question` 确认仅咨询还是生成部署方案" in supplement
    assert "确认前不得 `rejected`" in supplement
    assert "选择部署继续 Step1，明确仅咨询再分流" in supplement
    assert "尚无购买方案且咨询意图明确时" in supplement


@pytest.mark.parametrize("region", ["", "../cn-beijing", "cn-beijing/chat?token=bad", "https://example.com"])
def test_missing_or_invalid_session_region_uses_default(monkeypatch, region):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", region)
    with use_aliyun_credential(AliyunCredential(region_id=region)):
        supplement = build_prompt_context()["ros_agent_awareness"]
    assert f"https://ros.console.aliyun.com/{DEFAULT_REGION}/chat" in supplement
    assert "example.com" not in supplement
    assert "token=bad" not in supplement


def test_no_configured_credential_uses_default_region(monkeypatch):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setattr("iac_code.services.providers.aliyun.AliyunCredentials.load", lambda: None)
    assert f"https://ros.console.aliyun.com/{DEFAULT_REGION}/chat" in build_prompt_context()["ros_agent_awareness"]


def test_configured_region_is_used_without_a_session_override(monkeypatch):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setattr(
        "iac_code.services.providers.aliyun.AliyunCredentials.load",
        lambda: AliyunCredential(region_id="cn-shenzhen"),
    )
    assert "https://ros.console.aliyun.com/cn-shenzhen/chat" in build_prompt_context()["ros_agent_awareness"]


@pytest.mark.asyncio
async def test_pipeline_prompt_uses_each_concurrent_callers_console_region(executor, monkeypatch):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou")
    step = executor._pipeline.steps[0]
    context = PipelineContext(executor._pipeline.context_dependencies)

    async def prompt_for(region):
        with use_aliyun_credential(AliyunCredential(region_id=region)):
            await asyncio.sleep(0)
            return executor._build_full_system_prompt(step, context)

    beijing, shanghai = await asyncio.gather(prompt_for("cn-beijing"), prompt_for("cn-shanghai"))
    for prompt, own, other in [(beijing, "cn-beijing", "cn-shanghai"), (shanghai, "cn-shanghai", "cn-beijing")]:
        assert f"https://ros.console.aliyun.com/{own}/chat" in prompt
        assert f"https://ros.console.aliyun.com/{other}/chat" not in prompt
        assert "https://ros.console.aliyun.com/cn-hangzhou/chat" not in prompt
        base, skill_and_step = prompt.split("\n\n---\n\n", 1)
        assert "ros_agent_awareness" not in base
        assert "https://ros.console.aliyun.com/" not in base
        assert f"https://ros.console.aliyun.com/{own}/chat" in skill_and_step
        assert "ROS 控制台补充规则" in skill_and_step
        assert "ros_agent_awareness" not in prompt
        assert "明确咨询时，本规则优先于其他澄清、重新选择规则" in prompt
        assert "使用 `status: rejected`" in prompt


def test_handoff_is_limited_to_planning_and_leaves_normal_prompts_alone(executor, monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-beijing")
    context = PipelineContext(executor._pipeline.context_dependencies)
    prompts = [executor._build_full_system_prompt(step, context) for step in executor._pipeline.steps]
    assert "ROS 控制台补充规则" in prompts[0]
    assert prompts[0].index("ROS 控制台补充规则") > prompts[0].index("## Pipeline 交接约束")
    assert all("ROS 控制台补充规则" not in prompt for prompt in prompts[1:])
    assert "ROS 控制台补充规则" not in build_system_prompt(cwd=str(tmp_path))
    assert "部署架构与成本取舍留在当前流程" in prompts[0]
    assert "不得承诺优惠或会话迁移" in prompts[0]


def test_planning_keeps_existing_clarification_when_awareness_is_off(executor, monkeypatch):
    monkeypatch.delenv("IAC_CODE_ROS_AGENT_AWARENESS", raising=False)
    context = PipelineContext(executor._pipeline.context_dependencies)
    prompt = executor._build_full_system_prompt(executor._pipeline.steps[0], context)
    assert "ros_agent_awareness" not in prompt
    assert "ROS 控制台补充规则" not in prompt
    assert "https://ros.console.aliyun.com/" not in prompt
    assert "对于非部署/非云资源输入，应通过 `ask_user_question`" in prompt
    rendered_skill = prompt.split("\n\n---\n\n", 1)[1].rsplit("\n\n---\n\n", 1)[0]
    original_skill = executor._resolve_skill_prompt(executor._pipeline.steps[0].skill)
    assert rendered_skill == original_skill.replace("{ros_agent_awareness}", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tool-only", "intro", "visible", "chunked"])
async def test_guidance_is_visible_and_finishes_the_real_pipeline_without_candidates(tmp_path, monkeypatch, mode):
    """Script the model response; exercise actual tool execution and early exit."""
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IAC_CODE_CONFIG_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou")
    question = "ECS 实例 web+a&b#1 按量付费怎么计费？"
    destination = "https://ros.console.aliyun.com/cn-beijing/chat?" + urlencode({"prompt": question})
    guidance = f"请前往 [AI 助手]({destination}) 咨询按量付费。"

    class Model:
        def get_model_name(self):
            return "offline-ros-handoff"

        async def stream(self, messages, system, tools=None):
            assert "ROS 控制台补充规则" in system
            assert "https://ros.console.aliyun.com/cn-beijing/chat" in system
            yield MessageStartEvent(message_id="consultation")
            if mode == "intro":
                yield TextDeltaEvent(text="我来帮你生成一个可以直接打开的 AI 助手链接。")
            elif mode == "visible":
                yield TextDeltaEvent(text=guidance)
            elif mode == "chunked":
                for part in (guidance[:50], guidance[50:100], guidance[100:]):
                    yield TextDeltaEvent(text=part)
            yield ToolUseStartEvent(tool_use_id="handoff", name="complete_step")
            yield ToolUseEndEvent(
                tool_use_id="handoff",
                name="complete_step",
                input={"conclusion": {"status": "rejected", "rejection_reason": guidance}},
            )
            yield MessageEndEvent(stop_reason="tool_use", usage=Usage())

    flow = PipelineRunner(
        pipeline_dir=PIPELINE,
        provider_manager=Model(),
        base_tool_registry=ToolRegistry(),
        session_storage=SessionStorage(tmp_path / "projects"),
        session_id="consultation-handoff",
        cwd=str(tmp_path),
    )
    with use_aliyun_credential(AliyunCredential(region_id="cn-beijing")):
        events = [event async for event in flow.run(question)]
    visible = "".join(event.text for event in events if isinstance(event, TextDeltaEvent))
    assert visible.count(destination) == 1
    assert visible.endswith(guidance)
    tools = [event.name for event in events if isinstance(event, ToolUseStartEvent)]
    assert tools == ["complete_step"]
    conclusion = flow.context.get_conclusion("solution_selection")
    assert conclusion["status"] == "rejected"
    assert conclusion["continue_pipeline"] is False
    assert conclusion["rejection_reason"] == guidance
    assert flow.context.get_conclusion("selected_plan") is None
