"""Local-only HTTP fixture for the real resource-choice API and sidecar writer.

The launcher provides an isolated IAC_CODE_CONFIG_DIR and an empty project. No
agent turn is started, no credentials are loaded, and all resource facts are fake.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import socket
from pathlib import Path

import uvicorn
from resource_application_fixture import ResourceApplicationFixture
from starlette.responses import JSONResponse
from starlette.routing import Route

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.types.permissions import PermissionAuditMetadata, PermissionResult
from iac_code.types.stream_events import PermissionRequestEvent
from iac_code.web import resource_accounts, resource_queries
from iac_code.web.app import create_app
from iac_code.web.resource_references import resource_fact_to_web
from iac_code.web.session_manager import WebSessionManager


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    project = args.state_dir / "project"
    project.mkdir(parents=True, exist_ok=True)
    manager = WebSessionManager(projects_dir=args.state_dir / "projects", cwd=project)
    application_fixture = ResourceApplicationFixture(manager)
    load_stored_transcript = manager.load_visible_transcript
    session = manager.create_session(session_id="resource-choice-http-fixture")
    empty_session = manager.create_session(session_id="resource-empty-http-fixture")
    empty_transcript = {
        "messages": [
            {"messageId": "empty-role", "text": "RAM role EmptyIndexRole"},
            {"messageId": "empty-other", "text": "RAM role EmptyIndexRole"},
        ],
        "tools": {},
    }
    context_loads = 0

    def fixture_account_context():
        nonlocal context_loads
        context_loads += 1
        return {"account_scope": "credential:fake-empty-context", "console_partition": "aliyun"}

    resource_accounts.load_configured_resource_context = fixture_account_context
    seed = {
        "resourceType": "ALIYUN::RAM::Role",
        "identifier": "SeedRole",
        "identifierKind": "name",
        "identifierField": "RoleName",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
        "evidence": "api_response",
        "sources": [{"toolUseId": "seed-tool", "sourcePath": "/Role/RoleName"}],
    }
    transcript = {
        "messages": [
            {"messageId": "http-user-1", "text": "RAM role NewHttpRole"},
            {"messageId": "http-user-2", "text": "RAM role NewHttpRole"},
            {"messageId": "http-function", "text": "FC function HttpHandler"},
            {"messageId": "http-lifecycle-old", "text": "RAM role RecreatedRole"},
            {"messageId": "http-lifecycle-peer", "text": "RAM role RecreatedRole"},
        ],
        "tools": {
            "seed-tool": {
                "toolUseId": "seed-tool",
                "toolName": "aliyun_api",
                "input": {"product": "ram", "action": "GetRole", "params": {"RoleName": "SeedRole"}},
                "results": [{"content": '{"Role":{"RoleName":"SeedRole"}}', "resourceReferences": [seed]}],
            }
        },
    }
    lifecycle_progress = {
        "kind": "stack.progress",
        "toolUseId": "lifecycle-tool",
        "stackId": "fake-lifecycle-stack",
        "regionId": "cn-hangzhou",
        "accountScope": "fake-account",
        "consolePartition": "aliyun",
        "resources": [
            {
                "resource_type": "ALIYUN::RAM::Role",
                "name": "LogicalRole",
                "attributes": {"RoleName": "RecreatedRole"},
                "status": "CREATE_COMPLETE",
            }
        ],
    }
    transcript["tools"]["lifecycle-tool"] = {"results": [], "stackProgress": lifecycle_progress}
    query_calls: dict[str, int] = {}
    completed_reads: dict[str, int] = {}
    for name in (
        "LookupHandler",
        "RetryHandler",
        "PermissionHandler",
        "LateHandler",
        "ApprovalHandler",
        "RejectedHandler",
        "ReplacementHandler",
    ):
        tool_id = "query-tool-" + name
        facts = extract_api_resource_references(
            product="ros",
            action="GetStackResource",
            params={"StackId": "fake-query-stack"},
            body={
                "ResourceType": "ALIYUN::FC::Function",
                "PhysicalResourceId": name,
                "LogicalResourceId": name,
                "ResourceAttributes": {
                    "FunctionName": name,
                    **({"ServiceName": "OriginalService"} if name == "ReplacementHandler" else {}),
                },
            },
            region_id="cn-hangzhou",
            tool_use_id=tool_id,
            account_scope="fake-account",
            console_partition="aliyun",
        )
        transcript["messages"].append({"messageId": "http-query-" + name, "text": "FC function " + name})
        transcript["tools"][tool_id] = {
            "toolUseId": tool_id,
            "toolName": "aliyun_api",
            "input": {
                "product": "ros",
                "action": "GetStackResource",
                "params": {"StackId": "fake-query-stack", "LogicalResourceId": name},
            },
            "results": [
                {"content": '{"FunctionName":"' + name + '"}', "resourceReferences": resource_fact_to_web(facts)}
            ],
        }

    async def fixture_read(plan, **kwargs):
        query_calls[plan.identifier] = query_calls.get(plan.identifier, 0) + 1
        if plan.identifier == "RetryHandler" and query_calls[plan.identifier] == 1:
            raise resource_queries.ResourceQueryError("resource_query_timeout")
        if plan.identifier == "PermissionHandler":
            raise resource_queries.ResourceQueryError("resource_query_permission_required")
        if plan.identifier in {"ApprovalHandler", "RejectedHandler"} or plan.identifier.startswith(
            "ApplicationApproval"
        ):
            audit = PermissionAuditMetadata(scope="once", source="test", is_read_only=True)
            event = PermissionRequestEvent(
                tool_name="aliyun_api",
                tool_input=plan.tool_input(),
                tool_use_id="resource-query:fixture-" + plan.identifier,
                permission_result=PermissionResult(behavior="ask", audit=audit),
                audit_context={"metadata": audit, "cwd": kwargs["cwd"], "session_id": kwargs["session_id"]},
            )
            if not await kwargs["attempt"].request_permission(kwargs["permission_resolver"], event):
                raise resource_queries.ResourceQueryError("resource_query_permission_denied")
        if plan.identifier == "LateHandler":
            import asyncio

            await asyncio.sleep(0.2)
        completed_reads[plan.identifier] = completed_reads.get(plan.identifier, 0) + 1
        return extract_api_resource_references(
            product=plan.product,
            action=plan.action,
            params=plan.tool_input()["params"],
            body={
                "StackId": plan.stack_id,
                "LogicalResourceId": plan.logical_resource_id,
                "ResourceType": plan.resource_type,
                "PhysicalResourceId": plan.identifier,
                "ResourceAttributes": {"FunctionName": plan.identifier, "ServiceName": "QueriedService"},
            },
            region_id=plan.region_id,
            tool_use_id="fixture-read",
            account_scope=plan.account_scope,
            console_partition=plan.console_partition,
        )

    # This process is a test-only server. Replace the reader before serving any
    # request, so even a broken UI cannot reach credentials or real cloud APIs.
    resource_queries.read_resource_attributes = fixture_read

    def visible_transcript(session_id, **kwargs):
        if session_id in {empty_session.session_id, empty_session.web_session_id}:
            return copy.deepcopy(empty_transcript)
        if session_id in {session.session_id, session.web_session_id}:
            return copy.deepcopy(transcript)
        return load_stored_transcript(session_id, **kwargs)

    manager.load_visible_transcript = visible_transcript

    def forbidden_runtime(*args, **kwargs):
        raise AssertionError("This fixture cannot start an agent turn")

    app = create_app(
        session_manager=manager,
        runtime_factory=forbidden_runtime,
        shell_runner_factory=lambda: object(),
        pipeline_action_runner_factory=lambda: object(),
    )

    async def fixture_state(request):
        if request.method == "POST":
            payload = await request.json()
            # Tests may replace already-visible text, not inject facts/accounts.
            for message in transcript["messages"]:
                if payload.get("messageId") == message["messageId"] and isinstance(payload.get("text"), str):
                    message["text"] = payload["text"]
            result = transcript["tools"]["query-tool-ReplacementHandler"]["results"][0]
            if payload.get("action") == "replace-result-metadata":
                for fact in result["resourceReferences"]:
                    fact["attributes"].pop("ServiceName", None)
            elif payload.get("action") == "replace-result-body":
                result.clear()
                result["content"] = "The former result is no longer available."
            elif payload.get("action") == "recreate-name":
                for second, status in ((1, "DELETE_COMPLETE"), (2, "CREATE_COMPLETE")):
                    lifecycle_progress["resources"][0]["status"] = status
                    session.resource_references.on_event(
                        {
                            "type": "pipeline.event",
                            "payload": copy.deepcopy(lifecycle_progress),
                            "createdAt": f"2026-09-09T00:00:0{second}Z",
                        }
                    )
                    await session.resource_references.flush()
            elif payload.get("action") == "restore-resource-controller":
                await session.resource_references.close()
                manager._attach_resource_references(session)
        return JSONResponse(
            {
                "sessionId": session.web_session_id,
                "messages": transcript["messages"],
                "queryCalls": query_calls,
                "completedReads": completed_reads,
                "contextLoads": context_loads,
                "emptySessionId": empty_session.web_session_id,
                "emptyMessages": empty_transcript["messages"],
            }
        )

    app.routes.append(Route("/fixture/resource-choice", fixture_state, methods=["GET", "POST"]))
    app.routes.append(application_fixture.route())
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    print(json.dumps({"port": listener.getsockname()[1], "pid": os.getpid()}), flush=True)
    try:
        uvicorn.Server(uvicorn.Config(app, log_level="critical")).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
