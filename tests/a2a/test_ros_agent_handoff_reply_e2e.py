"""Assistant handoff replies through a real A2A process and persisted step transcript."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import httpx
import pytest

from iac_code.agent.message import Message
from scripts.ci.wiki_auto_model import WikiModel
from tests.a2a.test_permission_prompt_e2e import Server

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def _serve(root: str, port: int, *, model_url: str):
    import uvicorn

    from iac_code.a2a.app import create_app
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.providers.manager import ProviderManager
    from iac_code.services.providers.aliyun import AliyunCredential
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry

    root = Path(root)
    os.environ.update(
        IAC_CODE_CONFIG_DIR=str(root / "config"), IAC_CODE_CONFIG_BACKUP_DIR=str(root / "backup"),
        IAC_CODE_MODE="pipeline", IAC_CODE_PIPELINE_NAME="selling_solution_first",
        IACCODE_A2A_ALLOWED_CWDS=str(root), IAC_CODE_ROS_AGENT_AWARENESS="true",
        IAC_CODE_WIKI_AUTO_SEARCH="false", IAC_CODE_AUTO_MEMORY="false",
        IAC_CODE_PROVIDER="openai", IAC_CODE_API_KEY="fixture-key",
        IAC_CODE_BASE_URL=model_url, IAC_CODE_MODEL="gpt-4o-mini",
    )

    def runtime(options):
        registry = ToolRegistry()
        manager = ProviderManager(
            model="gpt-4o-mini", credentials={"openai": "fixture-key"},
            provider_key_override="openai", base_url_override=model_url, ignore_llm_source=True,
        )

        async def close():
            pass

        return SimpleNamespace(
            session_id=options.session_id, provider_manager=manager, tool_registry=registry,
            aliyun_services=None, aclose=close,
            agent_loop=AgentLoop(
                provider_manager=manager, system_prompt="Answer briefly.", tool_registry=registry,
                session_storage=SessionStorage(), session_id=options.session_id,
                cwd=options.cwd, resume_messages=options.resume_messages,
            ),
        )

    connect = socket.socket.connect

    def loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The handoff contract must not contact external services")
        return connect(sock, address)

    with (
        patch.object(socket.socket, "connect", loopback_only),
        patch("iac_code.a2a.executor.create_agent_runtime", runtime),
        patch("iac_code.a2a.pipeline_executor.create_agent_runtime", runtime),
        patch("iac_code.services.providers.aliyun.AliyunCredentials.load",
              return_value=AliyunCredential(region_id="cn-beijing")),
    ):
        app = create_app(host="127.0.0.1", port=port, token=None, model="gpt-4o-mini",
                         auto_approve_permissions=False, persistence_dir=root / "a2a")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


class HandoffServer(Server):
    def start(self):
        self.process = multiprocessing.get_context("spawn").Process(
            target=_serve, args=(str(self.root), self.port), kwargs=self.options,
        )
        self.process.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.is_alive(), "Handoff A2A process exited during startup"
            try:
                if self.client.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.02)
        raise AssertionError("Handoff A2A process did not become ready")


def _envelopes(value):
    if isinstance(value, dict):
        if "eventType" in value:
            yield value
        else:
            for child in value.values():
                yield from _envelopes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _envelopes(child)


@pytest.mark.parametrize("mode", ["tool-only", "intro", "visible"])
def test_handoff_reply_visible_once_and_survives_restart(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    question = "我只咨询已有 ECS 的按量账单，不打算部署或创建资源。"
    link = "https://ros.console.aliyun.com/cn-beijing/chat?" + urlencode({"prompt": question})
    guidance = f"请前往 [AI 助手]({link}) 咨询账单。"
    text = {"tool-only": "", "intro": "我来帮你生成一个 AI 助手链接。", "visible": guidance}[mode]
    with WikiModel() as model:
        model.main_scripts.append({"text": text, "calls": [("complete_step", {"conclusion": {
            "status": "rejected", "rejection_reason": guidance,
        }})]})
        server = HandoffServer(tmp_path, model_url=model.url)
        try:
            server.start()
            events = list(_envelopes(server.send(question)))
            visible = "".join(e.get("data", {}).get("text", "") for e in events if e["eventType"] == "text_delta")
            assert visible.count(link) == 1
            completed = [e for e in events if e["eventType"] == "step_completed"]
            assert completed[-1]["data"]["conclusion"]["rejection_reason"] == guidance
            assert any(e["eventType"] == "pipeline_handoff_ready" for e in events)
            assert not any(e["eventType"] in {"step_failed", "pipeline_failed"} for e in events)
            assert [r["kind"] for r in model.requests] == ["main"], "display fallback must not call another model"
            before = {p: p.read_text(encoding="utf-8")
                      for p in (tmp_path / "config/projects").rglob("session.jsonl")}
            matching = []
            for content in before.values():
                messages = [Message(**json.loads(line)) for line in content.splitlines()
                            if json.loads(line).get("role") == "assistant"]
                saved_text = "".join(m.get_text() for m in messages)
                if link in saved_text:
                    matching.append(saved_text)
            assert matching and all(t.count(link) == 1 for t in matching), "history must contain the visible reply once"
            server.stop()
            server.start()
            assert all(p.read_text(encoding="utf-8") == content for p, content in before.items())
            assert len(model.requests) == 1, "history recovery must not regenerate the handoff"
        finally:
            server.stop()
            server.client.close()
