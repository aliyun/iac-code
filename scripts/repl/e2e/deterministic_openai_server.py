from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ALIYUN_PROMPT_MARKER = "[E2E_ALIYUN]"
PIPELINE_PROMPT_MARKER = "[E2E_PIPELINE]"
FINAL_RESPONSE = "E2E_RESPONSE_OK"
FIXTURE_MODEL = "e2e-fixture-model"

PIPELINE_TEMPLATE_PATH = "templates/1-contract-vpc.yml"
PIPELINE_TEMPLATE = """ROSTemplateFormatVersion: '2015-09-01'
Description: Contract E2E VPC
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      VpcName: contract-e2e-vpc
      CidrBlock: 172.16.0.0/12
Outputs:
  VpcId:
    Description: VPC ID
    Label: VPC ID
    Value:
      Fn::GetAtt:
        - Vpc
        - VpcId
"""

PIPELINE_VSWITCH_TEMPLATE_PATH = "templates/1-vswitch-in-existing-vpc.yml"
PIPELINE_VSWITCH_TEMPLATE = """ROSTemplateFormatVersion: '2015-09-01'
Description: Contract E2E VSwitch in an existing VPC
Parameters:
  VpcId:
    Type: String
    Default: vpc-e2e-fixture
  ZoneId:
    Type: String
    Default: cn-hangzhou-h
  CidrBlock:
    Type: String
    Default: 172.16.1.0/24
Resources:
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VpcId
      ZoneId:
        Ref: ZoneId
      CidrBlock:
        Ref: CidrBlock
Outputs:
  VSwitchId:
    Description: VSwitch ID
    Label: VSwitch ID
    Value:
      Fn::GetAtt:
        - VSwitch
        - VSwitchId
"""


class DeterministicOpenAIServer:
    """A narrow OpenAI-compatible SSE endpoint for contract E2E scenarios."""

    def __init__(self, capture_path: Path, *, response_delay: float = 0.0) -> None:
        self.capture_path = Path(capture_path)
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.capture_path = self.capture_path  # type: ignore[attr-defined]
        self.httpd.capture_lock = threading.Lock()  # type: ignore[attr-defined]
        self.httpd.response_delay = max(0.0, response_delay)  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="e2e-openai-server", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> "DeterministicOpenAIServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def requests(self) -> list[dict[str, Any]]:
        if not self.capture_path.exists():
            return []
        return [json.loads(line) for line in self.capture_path.read_text(encoding="utf-8").splitlines() if line]

    def __enter__(self) -> "DeterministicOpenAIServer":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()


class _Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._capture(payload)
        response_delay = float(getattr(self.server, "response_delay", 0.0) or 0.0)
        if response_delay:
            time.sleep(response_delay)
        if payload.get("stream") is not True:
            self._write_non_streaming_response()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        messages = payload.get("messages") if isinstance(payload, dict) else None
        last_message = messages[-1] if isinstance(messages, list) and messages else {}
        last_role = last_message.get("role") if isinstance(last_message, dict) else None
        last_user_text = _last_user_text(messages if isinstance(messages, list) else [])
        try:
            if _pipeline_step_present(messages if isinstance(messages, list) else []):
                self._write_pipeline_response(payload)
            elif last_role == "tool":
                self._write_text_response()
            elif ALIYUN_PROMPT_MARKER in last_user_text:
                self._write_tool_response()
            else:
                self._write_text_response()
            self._write_usage()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Cancellation scenarios intentionally close the streaming response.
            return

    def _write_non_streaming_response(self) -> None:
        body = json.dumps(
            {
                "id": "chatcmpl-e2e-title",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "E2E contract chat"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _capture(self, payload: dict[str, Any]) -> None:
        capture_path: Path = getattr(self.server, "capture_path")
        lock: threading.Lock = getattr(self.server, "capture_lock")
        with lock, capture_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write_tool_response(self) -> None:
        arguments = json.dumps(
            {
                "product": "Vpc",
                "version": "2016-04-28",
                "action": "DescribeVpcs",
                "region_id": "cn-hangzhou",
                "endpoint": "vpc.cn-hangzhou.aliyuncs.com",
                "params": {"PageSize": 10},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._event(
            {
                "id": "chatcmpl-e2e-tool",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_e2e_aliyun",
                                    "type": "function",
                                    "function": {"name": "aliyun_api", "arguments": arguments},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        self._event(
            {
                "id": "chatcmpl-e2e-tool",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
        )

    def _write_pipeline_response(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        system_text = _system_text(messages)
        called_tools = _called_tool_names(messages)
        target = _pipeline_target(messages)
        is_vswitch = target == "vswitch"
        template_path = PIPELINE_VSWITCH_TEMPLATE_PATH if is_vswitch else PIPELINE_TEMPLATE_PATH
        template = PIPELINE_VSWITCH_TEMPLATE if is_vswitch else PIPELINE_TEMPLATE
        candidate_name = "Contract VSwitch" if is_vswitch else "Contract VPC"
        product = "VSwitch" if is_vswitch else "VPC"

        if "步骤：意图解析" in system_text:
            if "complete_step" not in called_tools and not _has_nudge(messages):
                self._write_text_response("E2E_PIPELINE_REFUSAL")
                return
            self._write_named_tool_response(
                "complete_step",
                {
                    "conclusion": {
                        "is_infra_intent": True,
                        "confidence": "high",
                        "cloud_platform": "aliyun",
                        "business_type": "network",
                        "core_requirements": [f"create one {product}"],
                        "resource_intents": [{"product": product, "action": "create", "source": "user"}],
                        "non_functional": {"region_preference": "cn-hangzhou"},
                    }
                },
            )
            return

        if "步骤：架构规划" in system_text:
            self._write_named_tool_response(
                "complete_step",
                {
                    "conclusion": {
                        "candidates": [
                            {
                                "name": candidate_name,
                                "output_path": template_path,
                                "products": [product],
                                "resource_intents": [
                                    {"product": product, "action": "create", "source": "user"}
                                ],
                                "topology": (
                                    "One VSwitch in an existing VPC" if is_vswitch else "One isolated VPC"
                                ),
                                "monthly_estimate": "¥0/月",
                                "pros": ["simple"],
                                "cons": ["single network"],
                            }
                        ]
                    }
                },
            )
            return

        if "步骤：模板生成" in system_text:
            if "write_file" not in called_tools:
                self._write_named_tool_response(
                    "write_file",
                    {"file_path": template_path, "content": template},
                )
                return
            if "ros_validate_template" not in called_tools:
                self._write_named_tool_response(
                    "ros_validate_template",
                    {"template_url": template_path, "region_id": "cn-hangzhou"},
                )
                return
            self._write_named_tool_response(
                "complete_step",
                {
                    "conclusion": {
                        "template": template,
                        "file_path": template_path,
                        "region": "cn-hangzhou",
                        "description": f"Contract E2E {product} template",
                    }
                },
            )
            return

        if "步骤：成本预估" in system_text:
            self._write_named_tool_response(
                "complete_step",
                {
                    "conclusion": {
                        "monthly_estimate": "¥0/月",
                        "currency": "CNY",
                        "resources": [{"type": product, "cost": "¥0/月"}],
                        "template_fixed": False,
                        "deployment_parameters": {},
                        "preview_validation": {
                            "succeeded": False,
                            "error": f"not required for free {product}",
                        },
                        "api_raw_summary": "deterministic contract fixture",
                    }
                },
            )
            return

        if "步骤：方案确认与选择" in system_text:
            self._write_named_tool_response(
                "complete_step",
                {
                    "conclusion": {
                        "user_prompt": "请选择要部署的方案：",
                        "options": [
                            {
                                "name": candidate_name,
                                "summary": (
                                    "Create one VSwitch in an existing VPC."
                                    if is_vswitch
                                    else "Create one isolated VPC."
                                ),
                                "candidate_index": 0,
                            }
                        ],
                    }
                },
            )
            return

        if "步骤：部署" in system_text:
            if "ros_deploy" not in called_tools:
                self._write_named_tool_response(
                    "ros_deploy",
                    {
                        "action": "create",
                        "stack_name": "contract-e2e-vswitch",
                        "template_url": template_path,
                        "parameters": {},
                        "region_id": "cn-hangzhou",
                    },
                )
                return
            self._write_named_tool_response(
                "complete_step",
                {"conclusion": {"status": "success", "stack_id": "stack-e2e-fixture"}},
            )
            return

        self._write_text_response("E2E_PIPELINE_UNROUTED")

    def _write_named_tool_response(self, name: str, arguments: dict[str, Any]) -> None:
        call_id = "call_e2e_{}".format(name)
        self._event(
            {
                "id": "chatcmpl-e2e-{}".format(name),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        self._event(
            {
                "id": "chatcmpl-e2e-{}".format(name),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }
        )

    def _write_text_response(self, content: str = FINAL_RESPONSE) -> None:
        self._event(
            {
                "id": "chatcmpl-e2e-text",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}
                ],
            }
        )
        self._event(
            {
                "id": "chatcmpl-e2e-text",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )

    def _write_usage(self) -> None:
        self._event(
            {
                "id": "chatcmpl-e2e-usage",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": FIXTURE_MODEL,
                "choices": [],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8, "total_tokens": 128},
            }
        )

    def _event(self, payload: dict[str, Any]) -> None:
        self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
        self.wfile.flush()


def _last_user_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"
            )
    return ""


def _all_message_text(messages: list[Any]) -> str:
    return "\n".join(_message_text(message) for message in messages if isinstance(message, dict))


def _system_text(messages: list[Any]) -> str:
    return "\n".join(
        _message_text(message) for message in messages if isinstance(message, dict) and message.get("role") == "system"
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return ""


def _called_tool_names(messages: list[Any]) -> set[str]:
    names: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.add(function["name"])
    return names


def _has_nudge(messages: list[Any]) -> bool:
    text = _all_message_text(messages)
    return "还没有成功调用 complete_step" in text or "请立即调用 complete_step" in text


def _pipeline_step_present(messages: list[Any]) -> bool:
    system_text = _system_text(messages)
    return any(
        heading in system_text
        for heading in (
            "步骤：意图解析",
            "步骤：架构规划",
            "步骤：模板生成",
            "步骤：成本预估",
            "步骤：方案确认与选择",
            "步骤：部署",
        )
    )


def _pipeline_target(messages: list[Any]) -> str:
    message_text = _all_message_text(messages).casefold()
    if "vswitch" in message_text or "交换机" in message_text:
        return "vswitch"
    return "vpc"
