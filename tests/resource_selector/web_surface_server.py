"""Production Web app host for the browser resource-selector E2E suite.

The only test-only pieces are the deterministic cloud caller and two control
read endpoints.  Static assets, session routes, SSE, the blocking panel, the
ORE bundle, BFF query/answer routes, and SelectCloudResourceTool itself are the
production implementations.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Route

from iac_code.resource_selector.profiles import PROFILE_HASH, QueryOperation, get_profile
from iac_code.resource_selector.query import ResourceSelectorQueryService
from iac_code.resource_selector.tools import SelectCloudResourceTool
from iac_code.tools.base import ToolContext
from iac_code.types.stream_events import CloudResourceSelectionEvent
from iac_code.web.app import create_app
from iac_code.web.runtime import WebTurnRequest, _resource_selection_request_payload
from iac_code.web.session_manager import WebSession, WebSessionManager

ROOT = Path(__file__).resolve().parent
CASES = {}
for item in json.loads((ROOT / "e2e-cases.json").read_text(encoding="utf-8"))["cases"]:
    profile = get_profile(item["selectorId"])
    if profile is not None and profile.enabled:
        CASES[item["selectorId"]] = item
FIXTURES = json.loads((ROOT / "contract-fixtures.json").read_text(encoding="utf-8"))["fixtures"]


class SurfaceState:
    def __init__(self, manager: WebSessionManager, session: WebSession) -> None:
        self.manager = manager
        self.session = session
        self.active_selector_id: str | None = None
        self.fixtures_by_signature: dict[tuple[str, str], tuple[QueryOperation, dict[str, Any]]] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.calls: dict[str, list[dict[str, Any]]] = {}

    def select_case(self, selector_id: str) -> dict[str, Any]:
        case = CASES.get(selector_id)
        profile = get_profile(selector_id)
        if case is None or profile is None or not profile.enabled:
            raise ValueError("selector_profile_mismatch")
        fixtures: dict[tuple[str, str], tuple[QueryOperation, dict[str, Any]]] = {}
        for request in case["expectedRequests"]:
            operation = next(item for item in profile.operations if item.key == request["operationKey"])
            fixtures[(operation.product, operation.action)] = (
                operation,
                FIXTURES[request["fixtureId"]]["response"],
            )
        self.active_selector_id = selector_id
        self.fixtures_by_signature = fixtures
        self.calls[selector_id] = []
        self.results.pop(selector_id, None)
        return case

    async def caller(
        self,
        product: str,
        action: str,
        _region_id: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        operation, response = self.fixtures_by_signature[(product, action)]
        if self.active_selector_id is not None:
            self.calls[self.active_selector_id].append(
                {
                    "operationKey": operation.key,
                    "product": product,
                    "action": action,
                    "params": params,
                }
            )
        if operation.request_kind == "multiApi":
            return next(iter(response.values()))
        cloned = json.loads(json.dumps(response))
        if self.active_selector_id == "kms.key" and action == "ListKeys":
            cloned["PageNumber"] = 1
            cloned["PageSize"] = 100
            cloned["TotalCount"] = 65
            cloned["Keys"] = {
                "Key": [
                    {"KeyId": "value-kms-key", "KeyArn": "acs:kms:cn-hangzhou:test:key/value-kms-key"},
                    *[
                        {
                            "KeyId": "key-test{:04d}".format(index),
                            "KeyArn": "acs:kms:cn-hangzhou:test:key/key-test{:04d}".format(index),
                        }
                        for index in range(1, 65)
                    ],
                ]
            }
        if action == "ListFoldersForParent" and params.get("ParentFolderId") == "fd-test0001":
            cloned["Folders"] = {"Folder": []}
            cloned["TotalCount"] = 0
        return cloned


class SelectorToolRuntime:
    def __init__(self, state: SurfaceState) -> None:
        self.state = state

    async def start_turn(self, request: WebTurnRequest) -> dict[str, Any]:
        selector_id = request.text.removeprefix("selector:").strip()
        case = self.state.select_case(selector_id)
        session = self.state.session
        turn_id = request.turn_id or "surface-{}".format(uuid.uuid4().hex)
        async with session.turn_lock:
            session.active_turn_task = asyncio.current_task()
            try:
                await session.events.publish(
                    "user.message",
                    {
                        "turnId": turn_id,
                        "messageId": "user-{}".format(turn_id),
                        "text": request.text,
                        "imageIds": [],
                        "fileRefs": [],
                    },
                )
                queue: asyncio.Queue[Any] = asyncio.Queue()
                tool = SelectCloudResourceTool(lambda: "cn-hangzhou")
                tool_task = asyncio.create_task(
                    tool.execute(
                        tool_input={
                            "question": "请选择资源",
                            "selector_id": selector_id,
                            "association_property_metadata": case["metadata"],
                            **({"source": case["source"]} if case["source"] is not None else {}),
                        },
                        context=ToolContext(event_queue=queue, tool_use_id="tool-{}".format(selector_id)),
                    )
                )
                event = await queue.get()
                if not isinstance(event, CloudResourceSelectionEvent) or event.response_future is None:
                    raise RuntimeError("resource selector tool did not emit its input-required event")
                request_id = self.state.manager.add_resource_selection_request(
                    session,
                    _resource_selection_request_payload(event, turn_id=turn_id),
                    future=event.response_future,
                )
                try:
                    result = await tool_task
                finally:
                    self.state.manager.discard_resource_selection_request(
                        request_id,
                        session_id=session.session_id,
                    )
                parsed = json.loads(result.content)
                self.state.results[selector_id] = parsed
                await session.events.publish(
                    "assistant.message.start",
                    {"turnId": turn_id, "messageId": "assistant-{}".format(turn_id)},
                )
                await session.events.publish(
                    "assistant.text.delta",
                    {
                        "turnId": turn_id,
                        "messageId": "assistant-{}".format(turn_id),
                        "delta": json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                    },
                )
                await session.events.publish(
                    "assistant.message.end",
                    {"turnId": turn_id, "messageId": "assistant-{}".format(turn_id), "finishReason": "stop"},
                )
                await session.events.publish("turn.done", {"turnId": turn_id})
                return {"accepted": True, "turnId": turn_id, "inputConsumed": True}
            finally:
                if session.active_turn_task is asyncio.current_task():
                    session.active_turn_task = None


def build_app(root: Path):
    os.environ["IAC_CODE_CONFIG_DIR"] = str(root / "config")
    project = root / "project"
    project.mkdir(parents=True)
    manager = WebSessionManager(projects_dir=root / "sessions", cwd=project)
    session = manager.create_session(cwd=str(project))
    state = SurfaceState(manager, session)
    runtime = SelectorToolRuntime(state)
    app = create_app(
        session_manager=manager,
        runtime_factory=lambda _session: runtime,
        resource_selector_query_service=ResourceSelectorQueryService(state.caller),
    )

    async def bootstrap(_request):
        display_session_id = session.to_dict().get("webSessionId", session.session_id)
        return JSONResponse(
            {
                "sessionId": display_session_id,
                "runtimeSessionId": session.session_id,
                "profileHash": PROFILE_HASH,
                "selectorCount": len(CASES),
            }
        )

    async def result(request):
        selector_id = request.path_params["selector_id"]
        value = state.results.get(selector_id)
        return JSONResponse(
            {"ready": value is not None, "result": value, "calls": state.calls.get(selector_id, [])}
        )

    app.routes.insert(0, Route("/__e2e/bootstrap", bootstrap, methods=["GET"]))
    app.routes.insert(1, Route("/__e2e/results/{selector_id:path}", result, methods=["GET"]))
    return app


def main() -> None:
    temporary = tempfile.TemporaryDirectory(prefix="iac-code-web-selector-surface-")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(2048)
    port = sock.getsockname()[1]
    print(json.dumps({"port": port}), flush=True)
    try:
        config = uvicorn.Config(build_app(Path(temporary.name)), log_level="warning", lifespan="on")
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        sock.close()
        temporary.cleanup()


if __name__ == "__main__":
    main()
