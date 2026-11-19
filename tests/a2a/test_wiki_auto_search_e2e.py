"""Wiki contracts through a separate real A2A process and HTTP model endpoint."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from iac_code.a2a.persistence import A2APersistenceStore
from iac_code.services.session_storage import SessionStorage
from scripts.ci.wiki_auto_model import WikiModel
from tests.a2a.test_permission_handoff_process_e2e import assert_success
from tests.a2a.test_permission_prompt_e2e import Server, _permission, _reply

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def _cloud_services(root):
    """Only remote metadata/target transport responses are scripted; runtime checks stay real."""
    from types import MappingProxyType

    from iac_code.services.providers.aliyun import AliyunCredential
    from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
    from iac_code.tools.cloud.aliyun.runtime import create_aliyun_runtime_services

    actions = {
        "ValidateTemplate": {"Parameters": {}},
        "PreviewStack": {"Stack": {"Resources": []}},
        "GetTemplateEstimateCost": {
            "OriginalAmount": "1280",
            "TradeAmount": "1024",
            "Currency": "CNY",
            "Resources": [],
        },
    }

    def metadata(request):
        if request.url.path.endswith("products.json"):
            return httpx.Response(
                200,
                json={
                    "products": [
                        {"product": "ROS", "defaultVersion": "2019-09-10", "versions": ["2019-09-10"], "style": "RPC"}
                    ]
                },
            )
        action = request.url.path.split("/")[-2]
        assert action in actions, request.url
        parameters = [
            {"name": name, "in": "formData", "schema": {"type": "string"}}
            for name in ("TemplateBody", "RegionId", "StackName")
        ]
        return httpx.Response(
            200,
            json={
                "product": "ROS",
                "version": "2019-09-10",
                "action": action,
                "style": "RPC",
                "methods": ["POST"],
                "path": "/",
                "schemes": ["HTTPS"],
                "operationType": "read",
                "consumes": ["application/x-www-form-urlencoded"],
                "produces": ["application/json"],
                "security": [{"AK": []}],
                "parameters": parameters,
                "responses": {},
                "components": {"schemas": {}},
            },
        )

    class CloudTransport:
        def __init__(self, original):
            self.original = original

        async def execute(self, **kwargs):
            action = kwargs["contract"].action
            assert action in actions, "the fixture cannot perform cloud writes"
            with (root / "cloud-calls.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps({"action": action, "endpoint": kwargs["endpoint"].endpoint}) + "\n")
            body = {"RequestId": "offline-cloud", **actions[action]}
            return NormalizedApiResponse(
                status=200,
                headers=MappingProxyType({}),
                body=body,
                content_type="application/json",
                content_encoding=None,
                size=len(json.dumps(body)),
            )

        async def aclose(self):
            close = getattr(self.original, "aclose", None)
            if callable(close):
                await close()

    services = create_aliyun_runtime_services(
        cache_dir=root / "openmeta", openmeta_transport=httpx.MockTransport(metadata)
    )
    services.credential_provider = lambda: AliyunCredential(
        access_key_id="fixture-id", access_key_secret="fixture-key", region_id="cn-hangzhou"
    )
    services.transport_router._transports = {
        name: CloudTransport(transport) for name, transport in services.transport_router._transports.items()
    }
    return services


def _serve(root, port, *, model_url, enabled, wiki_root=None, pipeline=False):
    import uvicorn

    from iac_code.a2a.app import create_app
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.providers.manager import ProviderManager
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import ToolRegistry
    from iac_code.tools.grep import GrepTool
    from iac_code.tools.read_file import ReadFileTool
    from iac_code.tools.write_file import WriteFileTool
    from iac_code.types.permissions import PermissionMode, ToolPermissionContext

    root = Path(root)
    os.environ.update(
        IAC_CODE_CONFIG_DIR=str(root / "config"),
        IAC_CODE_MODE="pipeline" if pipeline else "normal",
        IAC_CODE_CONFIG_BACKUP_DIR=str(root / "backup"),
        IAC_CODE_WIKI_AUTO_SEARCH=str(enabled).lower(),
        IACCODE_A2A_ALLOWED_CWDS=str(root),
        IAC_CODE_AUTO_MEMORY="false",
        IAC_CODE_PROVIDER="openai",
        IAC_CODE_API_KEY="fixture-key",
        IAC_CODE_BASE_URL=model_url,
        IAC_CODE_MODEL="gpt-4o-mini",
    )
    if enabled is None:
        os.environ.pop("IAC_CODE_WIKI_AUTO_SEARCH", None)
    if pipeline:
        os.environ["IAC_CODE_PIPELINE_NAME"] = "selling_solution_first"

    execute_grep = GrepTool.execute

    async def observed_grep(self, *, tool_input, context):
        result = await execute_grep(self, tool_input=tool_input, context=context)
        with (root / "searches.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"pattern": tool_input["pattern"], "is_error": result.is_error}) + "\n")
        return result

    def runtime(options):
        services = _cloud_services(root) if pipeline else None

        async def close():
            if services is not None:
                await services.aclose()

        registry = ToolRegistry()
        registry.register(GrepTool())
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())
        manager = ProviderManager(
            model="gpt-4o-mini",
            credentials={"openai": "fixture-key"},
            provider_key_override="openai",
            base_url_override=model_url,
            ignore_llm_source=True,
        )
        return SimpleNamespace(
            session_id=options.session_id,
            provider_manager=manager,
            tool_registry=registry,
            aliyun_services=services,
            aclose=close,
            agent_loop=AgentLoop(
                provider_manager=manager,
                system_prompt="Answer the user briefly.",
                tool_registry=registry,
                session_storage=SessionStorage(),
                session_id=options.session_id,
                cwd=options.cwd,
                resume_messages=options.resume_messages,
                max_turns=5,
                permission_context=ToolPermissionContext(cwd=options.cwd, mode=PermissionMode.ACCEPT_EDITS),
            ),
        )

    from contextlib import ExitStack

    with ExitStack() as patches:
        connect = socket.socket.connect

        def loopback_only(sock, address):
            if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
                raise AssertionError("Wiki contracts cannot connect to external services")
            return connect(sock, address)

        patches.enter_context(patch.object(socket.socket, "connect", loopback_only))
        patches.enter_context(patch("iac_code.a2a.executor.create_agent_runtime", runtime))
        if pipeline:
            patches.enter_context(patch("iac_code.a2a.pipeline_executor.create_agent_runtime", runtime))
        patches.enter_context(patch.object(GrepTool, "execute", observed_grep))
        if wiki_root:
            patches.enter_context(
                patch(
                    "iac_code.services.wiki_auto_search.get_iac_code_application_root", lambda: Path(wiki_root).parent
                )
            )
        app = create_app(
            host="127.0.0.1",
            port=port,
            token=None,
            model="gpt-4o-mini",
            auto_approve_permissions=False,
            persistence_dir=root / "a2a",
            permission_wait={"resident_timeout_seconds": 0.1, "timeout_grace_seconds": 0},
        )
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


class WikiServer(Server):
    def start(self):
        self.process = multiprocessing.get_context("spawn").Process(
            target=_serve, args=(str(self.root), self.port), kwargs=self.options
        )
        self.process.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert self.process.is_alive(), "Wiki A2A process exited during startup"
            try:
                if self.client.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.02)  # Readiness polling, not a product timing assertion.
        raise AssertionError("Wiki A2A process did not become ready")


@pytest.mark.parametrize("enabled", [None, False], ids=["unset", "false"])
@pytest.mark.parametrize("pipeline", [False, True], ids=["normal", "step1"])
def test_off(tmp_path, monkeypatch, enabled, pipeline):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with WikiModel() as model:
        server = WikiServer(tmp_path, model_url=model.url, enabled=enabled, pipeline=pipeline)
        try:
            server.start()
            if pipeline:
                from tests.pipeline.selling_solution_first.test_candidate_batch_e2e import DETAIL, OUTLINE

                model.main_scripts.extend(
                    {"calls": [call]}
                    for call in [
                        ("show_architecture_plan", OUTLINE),
                        ("show_candidate_detail", DETAIL),
                        (
                            "complete_step",
                            {
                                "conclusion": {
                                    "status": "awaiting_selection",
                                    "intent": {
                                        "cloud_platform": "aliyun",
                                        "resource_intents": [{"product": "RDS", "action": "create"}],
                                        "hard_constraints": [],
                                    },
                                }
                            },
                        ),
                    ]
                )
            assert_success(server.send("What is Intel B70?"), "main answer")
        finally:
            server.stop()
            server.client.close()
        assert [r["kind"] for r in model.requests] == ["main"] * (3 if pipeline else 1)
        assert "<knowledge_search_results" not in str(model.requests)
        assert not (tmp_path / "searches.jsonl").exists()


def test_normal_order(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with WikiModel() as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True)
        try:
            server.start()
            model.release_helper.clear()
            first = pool.submit(server.send, "What is Intel B70?")
            assert model.helper_started.wait(10), "first input did not extract Wiki keywords"
            assert not model.main_started.is_set(), "first model request escaped the serial gate"
            model.release_helper.set()
            assert_success(first.result(20), "main answer")
            assert model.order[:2] == ["helper_done", "main_started"]
            assert "knowledge_search_results" in str(model.requests[-1]["payload"]["messages"])

            model.helper_started.clear()
            model.main_started.clear()
            model.release_helper.clear()
            second = pool.submit(server.send, "DescribePrice for ECS billing")
            assert model.main_started.wait(10), "follow-up was blocked on keyword extraction"
            assert_success(second.result(15), "main answer")
            assert model.helper_started.wait(10)
            model.release_helper.set()
        finally:
            model.release_helper.set()
            server.stop()
            server.client.close()
        assert not model.errors


def _saved(root):
    context = A2APersistenceStore(root / "a2a").load_context("context-1")
    return SessionStorage().load(context.cwd, context.session_id)


def _wait_for(check, description):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)  # Observe a concrete persisted/endpoint state, never assert elapsed speed.
    raise AssertionError(description)


def _searches(root):
    path = root / "searches.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _envelopes(value):
    if isinstance(value, dict):
        if "eventType" in value:
            yield value
        else:
            for item in value.values():
                yield from _envelopes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _envelopes(item)


def _conclusion(events):
    completed = [e for e in _envelopes(events) if e["eventType"] == "step_completed"]
    assert completed, "production complete_step did not finish"
    assert not any(e["eventType"] == "step_failed" for e in _envelopes(events)), events
    return completed[-1]["data"]["conclusion"]


@pytest.mark.parametrize("restart", [False, True], ids=["live", "restored"])
@pytest.mark.parametrize("use_option", [False, True], ids=["free-text", "option"])
def test_pipeline_clarification_wiki_input(tmp_path, monkeypatch, restart, use_option):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    original = "我要购买按量付费的tdx，可信计算服务器"
    reply = "Intel B70 显卡" if use_option else "B70是一款intel显卡"
    question = {
        "question": "请选择需要了解的硬件，也可以补充其他问题。",
        "options": [{"id": "gpu", "label": "Intel B70 显卡"}],
    }
    with WikiModel(keywords=["TDX"]) as model:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True, pipeline=True)
        try:
            server.start()
            model.main_scripts.append({"calls": [("ask_user_question", question)]})
            events = server.send(original)
            assert any(e["eventType"] == "input_required" and e["data"].get("kind") == "ask_user_question"
                       for e in _envelopes(events))
            assert model.wait_requests(helper=1, main=1)
            if restart:
                server.stop()
                server.start()
            model.helper_scripts.append({"keywords": ["B70"]})
            model.main_scripts.extend([
                {"calls": [("read_file", {"path": "knowledge://common/hardware/intel-arc-pro-b70.md"})]},
                {"calls": [("complete_step", {"conclusion": {
                    "status": "rejected",
                    "rejection_reason": "This is a hardware consultation; no deployment is requested.",
                }})]},
            ])
            assert _conclusion(server.send("gpu" if use_option else reply))["status"] == "rejected"
            assert model.wait_requests(helper=2)
            helpers = [r["payload"] for r in model.requests if r["kind"] == "helper"]
            extraction = json.loads(helpers[1]["messages"][-1]["content"])
            assert extraction["input"] == reply, "Wiki must extract the accepted answer, not the original purchase"
            assert len(helpers) == 2, "one answer must not create duplicate Wiki queries"
            mains = [r["payload"] for r in model.requests if r["kind"] == "main"]
            assert any("intel-arc-pro-b70.md" in json.dumps(m["messages"]) for m in mains[1:])
            assert any("\"free_text\"" in str(m["messages"]) or "selected_label" in str(m["messages"])
                       for m in mains[1:]), "the existing tool-result answer protocol must remain intact"
        finally:
            model.release_helper.set()
            server.stop()
            server.client.close()


@pytest.mark.parametrize("keywords", [["RDS"], []], ids=["hits", "empty"])
def test_pipeline_order(tmp_path, monkeypatch, keywords):
    from iac_code.pipeline.engine.ui_contract import encode_deployment_confirmation, encode_selected_candidate
    from tests.pipeline.selling_solution_first.test_candidate_batch_e2e import DETAIL, OUTLINE

    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    initial = [
        ("show_architecture_plan", OUTLINE),
        ("show_candidate_detail", DETAIL),
        (
            "complete_step",
            {
                "conclusion": {
                    "status": "awaiting_selection",
                    "intent": {
                        "cloud_platform": "aliyun",
                        "resource_intents": [{"product": "RDS", "action": "create"}],
                        "hard_constraints": [],
                    },
                }
            },
        ),
    ]
    with WikiModel(keywords=keywords) as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True, pipeline=True)
        try:
            server.start()
            model.release_helper.clear()
            model.main_scripts.extend({"calls": [call]} for call in initial)
            running = pool.submit(server.send, "Plan a managed RDS database in Hangzhou; do not deploy")
            assert model.wait_requests(helper=1)
            assert not model.main_started.is_set(), "Step1 escaped serial Wiki preparation"
            model.release_helper.set()
            first_events = running.result(25)
            first = _conclusion(first_events)
            assert first["status"] == "awaiting_selection"
            wiki_events = [e for e in _envelopes(first_events) if e["eventType"] == "wiki_search"]
            assert wiki_events and wiki_events[-1]["data"]["snapshot"]["dispatch_mode"] == "serial"
            assert len([r for r in model.requests if r["kind"] == "helper"]) == 1

            model.release_helper.clear()
            model.main_started.clear()
            model.main_scripts.append({"calls": [("complete_step", {"conclusion": {}})]})
            followup = pool.submit(server.send, "Explain the database backup behavior")
            assert model.main_started.wait(10), "Step1 fresh-context followup waited on Wiki"
            unchanged = _conclusion(followup.result(20))
            assert unchanged["candidate_set_id"] == first["candidate_set_id"]
            assert model.wait_requests(helper=2)
            model.release_helper.set()
            assert model.wait_completed(helper=2)

            path = first["candidates"][0]["output_path"]
            template = "ROSTemplateFormatVersion: '2015-09-01'\nResources: {}\n"
            calls = [("write_file", {"path": path, "content": template})]
            for name in ("ros_validate_template", "ros_preview_template", "ros_estimate_template_cost"):
                calls.append(
                    (
                        name,
                        {
                            "template_url": path,
                            "region_id": "cn-hangzhou",
                            **({"parameters": {}} if name != "ros_validate_template" else {}),
                            **({"stack_name": "offline-wiki-preview"} if name == "ros_preview_template" else {}),
                        },
                    )
                )
            calls.append(
                (
                    "complete_step",
                    {
                        "conclusion": {
                            "status": "awaiting_confirmation",
                            "solution_summary": "Managed database",
                            "parameter_overrides": {},
                            "missing_deployment_parameters": [],
                            "hard_constraint_checks": [],
                        }
                    },
                )
            )
            model.main_scripts.extend({"calls": [call]} for call in calls)
            model.release_helper.clear()
            model.main_started.clear()
            selecting = pool.submit(server.send, encode_selected_candidate(first["candidates"][0]["name"], 0))
            assert model.wait_requests(helper=3)
            assert not model.main_started.is_set(), "Step2 must have its own serial preparation"
            helper = [r["payload"] for r in model.requests if r["kind"] == "helper"][-1]
            assert "RDS" in str(helper["messages"])
            assert "IAC_CODE_SELECTED_CANDIDATE" not in str(helper["messages"])
            model.release_helper.set()
            second_events = selecting.result(30)
            recovered_permission = False
            for _ in range(4):
                permission = _permission(second_events, required=False)
                if permission is None:
                    break
                assert permission["toolName"] in {
                    "write_file",
                    "ros_validate_template",
                    "ros_preview_template",
                    "ros_estimate_template_cost",
                }, permission
                if not recovered_permission:
                    server.wait_for_permission_handoff(permission)
                    server.stop()
                    server.start()
                    recovered_permission = True
                resumed = server.send(_reply(permission), task_id=permission["requestTaskId"])
                assert sum(r["kind"] == "helper" for r in model.requests) == 3
                second_events = resumed
            (tmp_path / "step2-events.json").write_text(json.dumps(second_events), encoding="utf-8")
            second = _conclusion(second_events)
            assert second["status"] == "awaiting_confirmation", second_events
            assert (tmp_path / "workspace" / path).is_file()
            cloud = [
                json.loads(line)["action"]
                for line in (tmp_path / "cloud-calls.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            assert cloud == ["ValidateTemplate", "PreviewStack", "GetTemplateEstimateCost"]
            assert recovered_permission, "the case must exercise real cold recovery of a suspended tool batch"
            assert sum(r["kind"] == "helper" for r in model.requests) == 3

            server.stop()
            server.start()
            model.release_helper.clear()
            model.main_started.clear()
            model.main_scripts.append({"calls": [("complete_step", {"conclusion": {}})]})
            explaining = pool.submit(server.send, "Explain the current template estimate without changing it")
            assert model.main_started.wait(10), "cold Step2 followup reset the serial gate"
            assert _conclusion(explaining.result(20))["status"] == "awaiting_confirmation"
            assert model.wait_requests(helper=4)
            model.release_helper.set()
            assert model.wait_completed(helper=4)
            count = len(model.requests)
            canceled = server.send(encode_deployment_confirmation("cancel"))
            assert not any(
                e["eventType"] == "step_started" and e.get("step", {}).get("id") == "deploying"
                for e in _envelopes(canceled)
            )
            assert len(model.requests) == count, "cancel control must not call the model or Wiki extractor"
            model.release_helper.clear()
            model.main_started.clear()
            normal = pool.submit(server.send, "What is Intel B70 after the pipeline handoff?")
            assert model.wait_requests(helper=5)
            assert not model.main_started.is_set(), "pipeline history incorrectly consumed normal's first input"
            model.release_helper.set()
            assert_success(normal.result(20), "main answer")
            normal_record = next(
                m.metadata["wiki_auto_search"]
                for m in reversed(_saved(tmp_path))
                if m.content == "What is Intel B70 after the pipeline handoff?"
            )
            assert normal_record["dispatch_mode"] == "serial"
            model.release_helper.clear()
            model.main_started.clear()
            following = pool.submit(server.send, "Explain B70 again")
            assert model.main_started.wait(10), "normal continuation after handoff must remain asynchronous"
            assert_success(following.result(20), "main answer")
            assert model.wait_requests(helper=6)
            if not keywords:
                assert not _searches(tmp_path)
                assert "<knowledge_search_results" not in str(model.requests)
                assert all(e["data"]["phase"] != "searching" for e in wiki_events)
        finally:
            model.release_helper.set()
            server.stop()
            server.client.close()


def test_late_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    old_gate, current_gate = threading.Event(), threading.Event()
    with WikiModel() as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True)
        try:
            server.start()
            assert_success(server.send("What is Intel B70?"), "main answer")
            model.helper_scripts.append({"keywords": ["TDX"], "gate": old_gate})
            assert_success(server.send("Explain TDX memory"), "main answer")
            assert model.wait_requests(helper=2, main=2)
            prior_states = {t.task_id: t.state for t in A2APersistenceStore(tmp_path / "a2a").list_tasks()}
            assert model.completed.count("helper") == 1, "old helper must still be gated after its main answer"
            model.helper_scripts.append({"keywords": ["DescribePrice"]})
            model.main_scripts.append(
                {
                    "gate": current_gate,
                    "calls": [
                        (
                            "read_file",
                            {
                                "path": str(
                                    Path(__file__).resolve().parents[2]
                                    / "src/iac_code/knowledge/clouds/aliyun/products/ecs/index.md"
                                )
                            },
                        )
                    ],
                }
            )
            current = pool.submit(server.send, "Explain DescribePrice for ECS")
            assert model.wait_requests(helper=3, main=3)
            _wait_for(lambda: any("describeprice" in s["pattern"].casefold() for s in _searches(tmp_path)),
                      "current search")
            old_gate.set()
            _wait_for(lambda: any("tdx" in s["pattern"].casefold() for s in _searches(tmp_path)),
                      "late search was discarded")
            current_gate.set()
            assert_success(current.result(20), "main answer")
            mains = [r["payload"] for r in model.requests if r["kind"] == "main"]
            assert len(mains) == 4, "late result must not cause an extra answer"
            current_wire = json.dumps(mains[-1]["messages"])
            blocks = [
                part
                for m in mains[-1]["messages"]
                if isinstance(m.get("content"), list)
                for part in m["content"]
                if "<knowledge_search_results" in str(part)
            ]
            assert len(blocks) == 1 and "DescribePrice" in str(blocks[0]) and "TDX" not in str(blocks[0])
            assert "Explain TDX memory" in current_wire
            saved = _saved(tmp_path)
            assert [m.content for m in saved].count("Explain TDX memory") == 1
            assert [m.content for m in saved].count("Explain DescribePrice for ECS") == 1
            states = {t.task_id: t.state for t in A2APersistenceStore(tmp_path / "a2a").list_tasks()}
            assert all(states[task_id] == state for task_id, state in prior_states.items()), states
            assert set(states.values()) == {"input-required"}, "normal replies retain their existing continuation state"
        finally:
            old_gate.set()
            current_gate.set()
            server.stop()
            server.client.close()


@pytest.mark.parametrize("keywords", [["B70"], []], ids=["ready", "skipped"])
@pytest.mark.parametrize("restore_enabled", [False, True], ids=["switch-off", "stay-on"])
def test_recovery(tmp_path, monkeypatch, keywords, restore_enabled):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with WikiModel(keywords=keywords) as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True)
        try:
            server.start()
            assert_success(server.send("What is Intel B70?"), "main answer")
            original = _saved(tmp_path)[0].metadata["wiki_auto_search"]["query_id"]
            task = A2APersistenceStore(tmp_path / "a2a").list_tasks()[0]
            server.stop()
            server.start()
            result = server.client.post(
                "/",
                headers={"A2A-Version": "1.0"},
                json={"jsonrpc": "2.0", "id": "history", "method": "GetTask", "params": {"id": task.task_id}},
            ).json()
            assert "error" not in result, result
            assert len(model.requests) == 2, "opening completed history must not prepare Wiki again"
            model.main_started.clear()
            model.release_helper.clear()
            repeated = pool.submit(server.send, "What is Intel B70?")
            assert model.main_started.wait(10), "cold recovery reset the normal first-input scope"
            assert_success(repeated.result(15), "main answer")
            assert model.wait_requests(helper=2, main=2)
            saved = _saved(tmp_path)
            inputs = [m for m in saved if m.content == "What is Intel B70?"]
            assert len(inputs) == 2, "same-text new input must survive recovery dedup"
            assert inputs[0].metadata["wiki_auto_search"]["query_id"] == original
            assert inputs[1].metadata["wiki_auto_search"]["query_id"] != original
            assert inputs[1].metadata["wiki_auto_search"]["dispatch_mode"] == "async"
            pending_task = max(
                A2APersistenceStore(tmp_path / "a2a").list_tasks(), key=lambda task: task.updated_at
            ).task_id
            server.stop()
            server.options["enabled"] = restore_enabled
            server.start()
            history = server.client.post(
                "/",
                headers={"A2A-Version": "1.0"},
                json={"jsonrpc": "2.0", "id": "pending-history", "method": "GetTask", "params": {"id": pending_task}},
            ).json()
            assert "error" not in history
            assert len(model.requests) == 4, "an answered turn's interrupted helper must not be replayed"
            model.main_started.clear()
            new_turn = pool.submit(server.send, "Now explain B70 briefly")
            assert model.main_started.wait(10)
            assert_success(new_turn.result(15), "main answer")
            last = model.requests[-1]["payload"]
            if not restore_enabled:
                assert "<knowledge_search_results" not in str(last["messages"])
            assert model.wait_requests(helper=3 if restore_enabled else 2, main=3)
            assert sum(r["kind"] == "helper" for r in model.requests) == (3 if restore_enabled else 2)
        finally:
            model.release_helper.set()
            server.stop()
            server.client.close()


def _continue_saved_input(root, model_url, finish):
    """Exercise the public normal-loop continuation, without reviving an A2A task.

    Normal A2A does not resurrect arbitrary killed executions. Its durable
    permission path is covered above; here the transcript produced by the real
    server is continued through the same public loop API used by resume paths.
    """
    import asyncio

    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.providers.manager import ProviderManager
    from iac_code.tools.base import ToolRegistry

    root = Path(root)
    os.environ["IAC_CODE_CONFIG_DIR"] = str(root / "config")
    os.environ["IAC_CODE_WIKI_AUTO_SEARCH"] = "true"
    os.environ["IAC_CODE_AUTO_MEMORY"] = "false"
    os.environ["IAC_CODE_MODEL"] = "gpt-4o-mini"
    os.environ["IAC_CODE_PROVIDER"] = "openai"
    os.environ["IAC_CODE_API_KEY"] = "fixture-key"
    os.environ["IAC_CODE_BASE_URL"] = model_url
    context = A2APersistenceStore(root / "a2a").load_context("context-1")
    storage = SessionStorage()
    manager = ProviderManager(
        model="gpt-4o-mini",
        credentials={"openai": "fixture-key"},
        provider_key_override="openai",
        base_url_override=model_url,
        ignore_llm_source=True,
    )
    loop = AgentLoop(
        provider_manager=manager,
        system_prompt="Answer briefly.",
        tool_registry=ToolRegistry(),
        session_storage=storage,
        session_id=context.session_id,
        cwd=context.cwd,
        resume_messages=storage.load(context.cwd, context.session_id),
    )

    async def run():
        events = [event async for event in loop.continue_streaming()]
        (root / "continued.json").write_text(
            json.dumps({"answered": any(getattr(event, "text", "") == "main answer" for event in events)}),
            encoding="utf-8",
        )
        # A turn ending is not application shutdown. Keep the real loop alive
        # while the parent observes the independent late preparation finish.
        await asyncio.to_thread(finish.wait)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["serial-pending", "serial-ready", "async-pending"])
def test_interrupted_recovery(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with WikiModel() as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True)
        old_helper, old_main = (threading.Event() for _ in range(2))
        finish = multiprocessing.get_context("spawn").Event()
        child = None
        try:
            server.start()
            asynchronous = mode.startswith("async")
            if asynchronous:
                assert_success(server.send("An earlier B70 question"), "main answer")
            initial_helpers = int(asynchronous)
            initial_mains = int(asynchronous)
            if mode != "serial-ready":
                model.helper_scripts.append({"gate": old_helper})
            model.main_scripts.append({"gate": old_main})
            original = pool.submit(server.send, "Explain B70")
            assert model.wait_requests(helper=initial_helpers + 1)
            if mode != "serial-pending":
                assert model.wait_requests(main=initial_mains + 1)
            saved = _wait_for(lambda: [m for m in _saved(tmp_path) if m.content == "Explain B70"], "input not saved")
            query = saved[0].metadata["wiki_auto_search"]["query_id"]
            server.stop()
            try:
                original.result(10)
            except httpx.TransportError:
                pass  # Deliberately killed the real server during an outstanding request.
            if mode == "serial-pending":
                model.main_scripts.clear()  # The killed first request never reached its scripted main response.
            historical = next(m for m in _saved(tmp_path) if m.content == "Explain B70").metadata["wiki_auto_search"]
            child = multiprocessing.get_context("spawn").Process(
                target=_continue_saved_input, args=(str(tmp_path), model.url, finish)
            )
            child.start()
            expected_mains = initial_mains + (1 if mode == "serial-pending" else 2)
            assert model.wait_requests(main=expected_mains), "historical preparation blocked recovery"
            _wait_for(lambda: (tmp_path / "continued.json").exists(), "recovery did not answer")
            assert sum(r["kind"] == "helper" for r in model.requests) == initial_helpers + 1
            finish.set()
            child.join(15)
            assert child.exitcode == 0
            assert json.loads((tmp_path / "continued.json").read_text(encoding="utf-8"))["answered"]
            saved = [m for m in _saved(tmp_path) if m.content == "Explain B70"]
            assert len(saved) == 1, "continuing the original input appended it twice"
            assert saved[0].metadata["wiki_auto_search"]["query_id"] == query
            assert saved[0].metadata["wiki_auto_search"]["dispatch_mode"] == ("async" if asynchronous else "serial")
            assert saved[0].metadata["wiki_auto_search"] == historical, "recovery rewrote historical navigation"
            resumed = [request for request in model.requests if request["kind"] == "main"][-1]
            assert ("<knowledge_search_results" in json.dumps(resumed)) == (mode == "serial-ready")
        finally:
            finish.set()
            for gate in (old_helper, old_main):
                gate.set()
            if child is not None:
                if child.is_alive():
                    child.kill()
                child.join(10)
                child.close()
            server.stop()
            server.client.close()


def test_live_sse_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with WikiModel() as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True)
        try:
            server.start()
            model.release_helper.clear()
            with server.stream("What is B70?") as response:
                events = server.events(response)
                first = next(events)
                task = _wait_for(lambda: A2APersistenceStore(tmp_path / "a2a").list_tasks(), "task not saved")[0]
                assert "error" not in first
                assert model.helper_started.wait(10)

            attached = threading.Event()

            def subscribe():
                with server.client.stream(
                    "POST",
                    "/",
                    headers={"A2A-Version": "1.0"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "reconnect",
                        "method": "SubscribeToTask",
                        "params": {"id": task.task_id},
                    },
                ) as response:
                    events = server.events(response)
                    first = next(events)
                    attached.set()
                    return [first, *events]

            subscribed = pool.submit(subscribe)
            assert attached.wait(10), "SSE subscriber did not attach to the live task"
            model.release_helper.set()
            received = subscribed.result(20)
            assert_success(received, "main answer")
            assert model.wait_requests(helper=1, main=1)
            assert sum(r["kind"] == "helper" for r in model.requests) == 1
            assert len([m for m in _saved(tmp_path) if m.content == "What is B70?"]) == 1
        finally:
            model.release_helper.set()
            server.stop()
            server.client.close()


@pytest.mark.parametrize(
    "mode",
    [
        "serial-cancel",
        "async-cancel",
        "invalid-json",
        "helper-error",
        "search-error",
        "async-invalid-json",
        "async-helper-error",
        "async-search-error",
    ],
)
def test_cancel_error(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    wiki_root = tmp_path / "workspace/app/knowledge"
    wiki_root.mkdir(parents=True)
    (wiki_root / "facts.md").write_text("# GPU\n\nB70\n", encoding="utf-8")
    with WikiModel() as model, ThreadPoolExecutor(1) as pool:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True, wiki_root=str(wiki_root))
        try:
            server.start()
            background = mode.startswith("async-")
            failure = mode.removeprefix("async-")
            if background:
                assert_success(server.send("Explain B70"), "main answer")
                model.helper_started.clear()
                model.main_started.clear()
                if mode == "async-cancel":
                    model.release_main.clear()
            if mode.endswith("cancel"):
                model.release_helper.clear()
                running = pool.submit(server.send, "Check B70 specifications")
                assert model.helper_started.wait(10)
                if mode == "serial-cancel":
                    assert not model.main_started.is_set()
                else:
                    assert model.main_started.wait(10)
                context = A2APersistenceStore(tmp_path / "a2a").load_context("context-1")
                result = server.client.post(
                    "/",
                    headers={"A2A-Version": "1.0"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "cancel",
                        "method": "CancelTask",
                        "params": {"id": context.active_task_id},
                    },
                ).json()
                assert "error" not in result, result
                running.result(15)
                model.release_helper.set()
                model.release_main.set()
                _wait_for(
                    lambda: _saved(tmp_path)[-1].metadata.get("wiki_auto_search", {}).get("status") == "canceled",
                    "Wiki cancellation was not persisted",
                )
                assert len(_searches(tmp_path)) == (1 if mode == "async-cancel" else 0)
                assert A2APersistenceStore(tmp_path / "a2a").load_task(context.active_task_id).state == "canceled"
                assert sum(r["kind"] == "main" for r in model.requests) == (2 if mode == "async-cancel" else 0)
            else:
                model.helper_scripts.append(
                    {"text": "invalid-json"}
                    if failure == "invalid-json"
                    else {"status": 400}
                    if failure == "helper-error"
                    else {}
                )
                if failure == "search-error" or background:
                    model.release_helper.clear()
                    running = pool.submit(server.send, "Check B70 specifications")
                    assert model.helper_started.wait(10)
                    if background:
                        events = running.result(15)
                        assert_success(events, "main answer")
                    if failure == "search-error":
                        (wiki_root / "facts.md").unlink()
                        wiki_root.rmdir()
                    model.release_helper.set()
                    events = running.result(20)
                else:
                    events = server.send("Check B70 specifications")
                assert_success(events, "main answer")
                _wait_for(
                    lambda: len(_searches(tmp_path)) == (2 if background else 1), "error fallback search did not finish"
                )
                record = next(
                    m.metadata["wiki_auto_search"]
                    for m in reversed(_saved(tmp_path))
                    if m.content == "Check B70 specifications"
                )
                expected = "failed" if failure == "search-error" else "fallback_ready"
                assert record["status"] in ({expected, "pending"} if background else {expected})
                assert sum(r["kind"] == "helper" for r in model.requests) == (2 if background else 1)
                assert sum(r["kind"] == "main" for r in model.requests) == (2 if background else 1)
        finally:
            model.release_helper.set()
            model.release_main.set()
            server.stop()
            server.client.close()


def test_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    with WikiModel(keywords=[]) as model:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True)
        try:
            server.start()
            assert_success(server.send("Thanks; just reformat the previous answer."), "main answer")
            saved = _saved(tmp_path)
            record = next(m.metadata["wiki_auto_search"] for m in saved if "wiki_auto_search" in m.metadata)
            assert record["status"] == "skipped" and record["hits"] == []
            assert "<knowledge_search_results" not in str(model.requests)
            assert [r["kind"] for r in model.requests] == ["helper", "main"]
            assert saved[0].content == "Thanks; just reformat the previous answer."
            assert_success(server.send("Please keep the same wording."), "main answer")
            _wait_for(lambda: model.completed.count("helper") == 2, "empty followup helper did not finish")
            assert not _searches(tmp_path)
            assert "<knowledge_search_results" not in str(model.requests)
        finally:
            server.stop()
            server.client.close()


@pytest.mark.parametrize("budget", [False, True], ids=["native", "budget"])
def test_request(tmp_path, monkeypatch, budget):
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "workspace").mkdir()
    kwargs = {}
    keywords = [{"text": "B70", "relevance": 0.9}]
    page = Path(__file__).resolve().parents[2] / "src/iac_code/knowledge/common/hardware/intel-arc-pro-b70.md"
    if budget:
        root = tmp_path / "workspace/app/knowledge"
        root.mkdir(parents=True)
        # Two distant keyword windows exceed the byte budget even with a short temporary path.
        keywords.append({"text": "TDX", "relevance": 0.7})
        for index in range(8):
            (root / f"page-{index}.md").write_text(
                '# ' + "显卡" * 80 + '\n\n' + "相关证据" * 100
                + " B70 SOURCE_LINE_MARKER " + "相关证据" * 100
                + " TDX " + "相关证据" * 100 + "\nUNRELATED_FULL_BODY\n", encoding="utf-8"
            )
        page = root / "page-0.md"
        kwargs["wiki_root"] = str(root)
    with WikiModel(keywords=keywords) as model:
        server = WikiServer(tmp_path, model_url=model.url, enabled=True, **kwargs)
        try:
            server.start()
            read_path = str(page) if budget else "knowledge://common/hardware/intel-arc-pro-b70.md"
            model.main_scripts.append({"calls": [("read_file", {"path": read_path})]})
            assert_success(server.send("What is Intel B70?"), "main answer")
            main = next(r["payload"] for r in model.requests if r["kind"] == "main")
            assert main["messages"][0]["content"] == "Answer the user briefly."
            wire = json.dumps(main["messages"][1:])
            assert wire.count("<knowledge_search_results") == 1
            assert page.name in wire
            if not budget:
                assert "# knowledge://common/hardware/intel-arc-pro-b70.md" in wire
                assert str(page.parent) not in wire
            body_marker = "SOURCE_LINE_MARKER" if budget else "32 GB GDDR6"
            assert body_marker in wire and "tool_result" not in wire
            assert "UNRELATED_FULL_BODY" not in wire, "only matched excerpts belong in automatic recall"
            mains = [r["payload"] for r in model.requests if r["kind"] == "main"]
            assert len(mains) == 2 and sum(r["kind"] == "helper" for r in model.requests) == 1
            assert json.dumps(mains[-1]["messages"]).count("<knowledge_search_results") == 1
            assert body_marker in json.dumps(mains[-1]["messages"]), "main model must execute a real read_file"
            block = next(
                part["text"]
                for m in main["messages"]
                if isinstance(m.get("content"), list)
                for part in m["content"]
                if "<knowledge_search_results" in part.get("text", "")
            )
            from iac_code.services.token_counter import TokenCounter

            assert len(block.encode("utf-8")) <= 6144
            assert TokenCounter("gpt-4o-mini").count_text(block) <= 1200
            if budget:
                assert 0 < block.count("\n# ") < 8, "over-budget excerpts must be dropped as whole lines"
                for line in block.splitlines():
                    if line.startswith("# "):
                        assert Path(line.removeprefix("# ")).is_file()
            saved = _saved(tmp_path)
            assert sum(m.content == "What is Intel B70?" for m in saved) == 1
            assert all("<knowledge_search_results" not in str(m.content) for m in saved)
            record = next(m.metadata["wiki_auto_search"] for m in saved if "wiki_auto_search" in m.metadata)
            assert record["status"] == "ready"
            assert record["keyword_weights"] == {item["text"]: item["relevance"] for item in keywords}
            assert all(not Path(hit["path"]).is_absolute() for hit in record["hits"])
        finally:
            server.stop()
            server.client.close()
