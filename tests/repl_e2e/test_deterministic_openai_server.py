from __future__ import annotations

import json

import pytest
from openai import AsyncOpenAI

from scripts.repl.e2e.deterministic_openai_server import (
    ALIYUN_PROMPT_MARKER,
    FINAL_RESPONSE,
    PIPELINE_PROMPT_MARKER,
    PIPELINE_TEMPLATE_PATH,
    PIPELINE_VSWITCH_TEMPLATE_PATH,
    DeterministicOpenAIServer,
)


@pytest.mark.asyncio
async def test_server_streams_a_tool_round_then_text(tmp_path) -> None:
    with DeterministicOpenAIServer(tmp_path / "requests.jsonl") as server:
        client = AsyncOpenAI(api_key="test", base_url=server.base_url)
        first = await client.chat.completions.create(
            model="fixture",
            messages=[{"role": "user", "content": ALIYUN_PROMPT_MARKER}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "aliyun_api", "description": "test", "parameters": {"type": "object"}},
                }
            ],
            stream=True,
        )
        chunks = [chunk async for chunk in first]
        tool_call = chunks[0].choices[0].delta.tool_calls[0]
        assert tool_call.function.name == "aliyun_api"
        assert json.loads(tool_call.function.arguments)["action"] == "DescribeVpcs"

        second = await client.chat.completions.create(
            model="fixture",
            messages=[{"role": "tool", "tool_call_id": "call_e2e_aliyun", "content": "{}"}],
            stream=True,
        )
        chunks = [chunk async for chunk in second]
        assert chunks[0].choices[0].delta.content == FINAL_RESPONSE
        await client.close()

    assert len(server.requests()) == 2


@pytest.mark.asyncio
async def test_server_routes_pipeline_nudge_and_template_tools(tmp_path) -> None:
    with DeterministicOpenAIServer(tmp_path / "requests.jsonl") as server:
        client = AsyncOpenAI(api_key="test", base_url=server.base_url)
        refusal = await _completion(
            client,
            [
                {"role": "system", "content": "# 步骤：意图解析"},
                {"role": "user", "content": PIPELINE_PROMPT_MARKER},
            ],
        )
        assert refusal[0].choices[0].delta.content == "E2E_PIPELINE_REFUSAL"

        intent = await _completion(
            client,
            [
                {"role": "system", "content": "# 步骤：意图解析"},
                {"role": "user", "content": PIPELINE_PROMPT_MARKER},
                {"role": "user", "content": "你还没有成功调用 complete_step，请立即调用 complete_step"},
            ],
        )
        intent_call = intent[0].choices[0].delta.tool_calls[0]
        assert intent_call.function.name == "complete_step"
        assert json.loads(intent_call.function.arguments)["conclusion"]["is_infra_intent"] is True

        template = await _completion(
            client,
            [
                {"role": "system", "content": "# 步骤：模板生成"},
                {"role": "user", "content": PIPELINE_PROMPT_MARKER},
                _tool_call_message("write_file"),
                {"role": "tool", "tool_call_id": "call_e2e_write_file", "content": "{}"},
            ],
        )
        template_call = template[0].choices[0].delta.tool_calls[0]
        assert template_call.function.name == "ros_validate_template"
        assert json.loads(template_call.function.arguments)["template_url"] == PIPELINE_TEMPLATE_PATH
        await client.close()


@pytest.mark.asyncio
async def test_server_routes_vswitch_deployment_to_ros_deploy_then_complete_step(tmp_path) -> None:
    with DeterministicOpenAIServer(tmp_path / "requests.jsonl") as server:
        client = AsyncOpenAI(api_key="test", base_url=server.base_url)
        deploy = await _completion(
            client,
            [
                {"role": "system", "content": "# 步骤：部署"},
                {"role": "user", "content": "选择已有 VPC 并创建 VSwitch"},
            ],
        )
        deploy_call = deploy[0].choices[0].delta.tool_calls[0]
        deploy_input = json.loads(deploy_call.function.arguments)
        assert deploy_call.function.name == "ros_deploy"
        assert deploy_input == {
            "action": "create",
            "stack_name": "contract-e2e-vswitch",
            "template_url": PIPELINE_VSWITCH_TEMPLATE_PATH,
            "parameters": {},
            "region_id": "cn-hangzhou",
        }

        complete = await _completion(
            client,
            [
                {"role": "system", "content": "# 步骤：部署"},
                {"role": "user", "content": "选择已有 VPC 并创建 VSwitch"},
                _tool_call_message("ros_deploy"),
                {
                    "role": "tool",
                    "tool_call_id": "call_e2e_ros_deploy",
                    "content": '{"stack_id":"stack-e2e-fixture"}',
                },
            ],
        )
        complete_call = complete[0].choices[0].delta.tool_calls[0]
        assert complete_call.function.name == "complete_step"
        assert json.loads(complete_call.function.arguments) == {
            "conclusion": {"status": "success", "stack_id": "stack-e2e-fixture"}
        }
        await client.close()


async def _completion(client: AsyncOpenAI, messages: list[dict]) -> list:
    response = await client.chat.completions.create(model="fixture", messages=messages, stream=True)
    return [chunk async for chunk in response]


def _tool_call_message(name: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_e2e_{}".format(name),
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }
