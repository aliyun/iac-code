"""Offline contracts for optional ROS sales-page guidance and request-local links."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from iac_code.agent.system_prompt import build_system_prompt
from iac_code.pipeline.engine.context import PipelineContext
from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.pipeline_runner import PipelineRunner
from iac_code.pipeline.engine.step_executor import StepExecutor
from iac_code.pipeline.selling_solution_first.hooks.solution_planning_and_selection import build_prompt_context
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
    assert "[AI Assistant](https://ros.console.aliyun.com/cn-shanghai/chat)" in supplement
    assert "{ai_assistant_destination}" not in supplement
    assert len(supplement) <= 800, "Keep the optional guidance short"
    assert "`?prompt=` INSIDE its URL" in supplement
    assert "standalone user question with needed context" in supplement
    assert "encode reserved characters" in supplement
    assert re.search(r"[\u4e00-\u9fff]", supplement) is None


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
        assert "ROS console override" in skill_and_step
        assert "ros_agent_awareness" not in prompt
        assert "override clarification/reselection" in prompt
        assert "with `status: rejected`" in prompt


def test_handoff_is_limited_to_planning_and_leaves_normal_prompts_alone(executor, monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_CODE_ROS_AGENT_AWARENESS", "true")
    monkeypatch.setenv("ALIBABA_CLOUD_REGION_ID", "cn-beijing")
    context = PipelineContext(executor._pipeline.context_dependencies)
    prompts = [executor._build_full_system_prompt(step, context) for step in executor._pipeline.steps]
    assert "ROS console override" in prompts[0]
    assert prompts[0].index("ROS console override") > prompts[0].index("## Pipeline 交接约束")
    assert all("ROS console override" not in prompt for prompt in prompts[1:])
    assert "ROS console override" not in build_system_prompt(cwd=str(tmp_path))
    assert "Keep deployment architecture/cost tradeoffs here" in prompts[0]
    assert "do not promise discounts or session transfer" in prompts[0]


def test_planning_keeps_existing_clarification_when_awareness_is_off(executor, monkeypatch):
    monkeypatch.delenv("IAC_CODE_ROS_AGENT_AWARENESS", raising=False)
    context = PipelineContext(executor._pipeline.context_dependencies)
    prompt = executor._build_full_system_prompt(executor._pipeline.steps[0], context)
    assert "ros_agent_awareness" not in prompt
    assert "ROS console override" not in prompt
    assert "https://ros.console.aliyun.com/" not in prompt
    assert "对于非部署/非云资源输入，应通过 `ask_user_question`" in prompt
    rendered_skill = prompt.split("\n\n---\n\n", 1)[1].rsplit("\n\n---\n\n", 1)[0]
    original_skill = executor._resolve_skill_prompt(executor._pipeline.steps[0].skill)
    assert rendered_skill == original_skill.replace("{ros_agent_awareness}", "")


@pytest.mark.asyncio
async def test_guidance_is_visible_and_finishes_the_real_pipeline_without_candidates(tmp_path, monkeypatch):
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
            assert "ROS console override" in system
            assert "https://ros.console.aliyun.com/cn-beijing/chat" in system
            yield MessageStartEvent(message_id="consultation")
            yield TextDeltaEvent(text=guidance)
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
    assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == guidance
    tools = [event.name for event in events if isinstance(event, ToolUseStartEvent)]
    assert tools == ["complete_step"]
    conclusion = flow.context.get_conclusion("solution_selection")
    assert conclusion["status"] == "rejected"
    assert conclusion["continue_pipeline"] is False
    assert conclusion["rejection_reason"] == guidance
    assert flow.context.get_conclusion("selected_plan") is None
