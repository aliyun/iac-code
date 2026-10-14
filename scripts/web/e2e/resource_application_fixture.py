"""Persisted sessions and deterministic events for full-application link checks.

Unlike the small choice fixture, these sessions use the unmodified session
loader, normal JSONL storage, pipeline journal, translator, and SSE buffer.
Only model/tool execution is replaced: every value below is a test literal.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from iac_code.a2a.persistence import A2AContextSnapshot, A2APersistenceStore, A2ATaskSnapshot
from iac_code.a2a.pipeline_journal import A2APipelineJournal
from iac_code.a2a.pipeline_paths import a2a_pipeline_dir_for_session
from iac_code.a2a.pipeline_snapshot import A2APipelineSnapshotStore, reduce_pipeline_events
from iac_code.agent.message import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from iac_code.config import get_config_dir
from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.base_stack import STACK_RESULT_METADATA_KEY
from iac_code.tools.cloud.resource_references import RESOURCE_REFERENCES_METADATA_KEY, resource_reference_metadata
from iac_code.web.pipeline_transcript import PipelineTranscriptTranslator
from iac_code.web.resource_reference_controller import _stack_facts
from iac_code.web.resource_references import resource_fact_to_web

ROLE = "ApplicationRole"
INSTANCE = "i-application000001"
STREAM_ID = "i-streaming000001"
STACK_ID = "11111111-2222-4333-8444-555555555556"
DEPLOY_ID = "i-deployment000001"
DEPLOY_ROLE = "DeploymentRole"
TEMPLATE = json.dumps(
    {
        "ROSTemplateFormatVersion": "2015-09-01",
        "Resources": {},
        "Metadata": {"RoleName": DEPLOY_ROLE, "InstanceId": DEPLOY_ID, "DisplayName": DEPLOY_ROLE},
        "PreviewChoice": {"RoleName": "PreviewOnlyRole"},
    },
    indent=2,
)
USER_TEXT = f"Application user: RAM role {ROLE}; {INSTANCE}."
THINKING = f"Application thinking: RAM role {ROLE}; {INSTANCE}."
BODY = "\n\n".join(
    [
        f"Application paragraph: RAM role {ROLE}; {INSTANCE}.",
        f"- Application list: RAM role {ROLE}; {INSTANCE}.",
        f"Application inline: RAM role `{ROLE}`; `{INSTANCE}`.",
        f"| RoleName | InstanceId |\n| --- | --- |\n| {ROLE} | {INSTANCE} |",
        "```json\n" + json.dumps({"RoleName": ROLE, "InstanceId": INSTANCE}) + "\n```",
        f"```yaml\nRoleName: {ROLE}\nInstanceId: {INSTANCE}\n```",
        f'```hcl\nresource "alicloud_ram_role" "logical" {{ name = "{ROLE}" }}\n# {INSTANCE}\n```',
        f"```shell\naliyun ram GetRole --RoleName {ROLE}\necho {INSTANCE}\n```",
        f"Negative controls: admin, {ROLE}Backup, ordinary {ROLE}.",
        "```json\n" + json.dumps({"DisplayName": ROLE, "LogicalResourceId": ROLE}) + "\n```",
    ]
)
TOOL_INPUT = {"RoleName": ROLE, "InstanceId": INSTANCE}
TOOL_OUTPUT = json.dumps({"RoleName": ROLE, "InstanceId": INSTANCE}, indent=2)
DISPLAY_TOOLS = [
    ("application-tool", "fixture_output", TOOL_INPUT, TOOL_OUTPUT),
    (
        "application-shell",
        "bash",
        {"command": f"aliyun ram GetRole --RoleName {ROLE}\necho {INSTANCE}"},
        f"STDOUT:\nRAM role {ROLE}; {INSTANCE}.\n\nSTDERR:\nRAM role {ROLE}; {INSTANCE}.\n\nExit code: 0",
    ),
    ("application-conclusion", "complete_step", {"conclusion": TOOL_INPUT}, "Completed"),
]


def recovery_names(mode):
    return {
        "role": "ApplicationChoice-" + mode,
        "refresh": "ApplicationApprovalRefresh-" + mode,
        "restart": "ApplicationApprovalRestart-" + mode,
    }


class ResourceApplicationFixture:
    def __init__(self, manager):
        self.manager = manager
        self.sessions = {}
        self.journals = {}
        self.translators = {}
        self.events_sent = {}
        for mode in ("normal", "selling", "selling_solution_first"):
            session = manager.create_session(
                session_id="resource-app-" + mode,
                mode="normal" if mode == "normal" else "pipeline",
                pipeline_name=None if mode == "normal" else mode,
            )
            self.sessions[mode] = session
            self.events_sent[mode] = []
            if mode != "normal":
                session.context_id = "ctx-resource-app-" + mode
                session.task_id = "task-resource-app-" + mode
                manager.persist_web_metadata(session)
                pipeline_id = "a2a-resource-app-" + mode
                A2APersistenceStore(get_config_dir() / "a2a").save_context(
                    A2AContextSnapshot(context_id=session.context_id, session_id=pipeline_id, cwd=session.cwd)
                )
                self.journals[mode] = A2APipelineJournal(
                    a2a_pipeline_dir_for_session(cwd=session.cwd, session_id=pipeline_id)
                )
                translator = PipelineTranscriptTranslator()
                translator.translate_all(self.journals[mode].read_all())
                self.translators[mode] = translator
            if not manager.storage.load(session.cwd, session.session_id):
                self.append_message(mode, Message(role="user", content=USER_TEXT))
                if mode == "normal":
                    self.append_message(
                        mode,
                        Message(
                            role="assistant",
                            content=[
                                ThinkingBlock(thinking=THINKING),
                                TextBlock(text=BODY),
                                *[
                                    ToolUseBlock(id=tool_id, name=name, input=tool_input)
                                    for tool_id, name, tool_input, _ in DISPLAY_TOOLS
                                ],
                            ],
                        ),
                    )
                    self.append_message(
                        mode,
                        Message(
                            role="user",
                            content=[
                                ToolResultBlock(tool_use_id=tool_id, content=content)
                                for tool_id, _, _, content in DISPLAY_TOOLS
                            ],
                        ),
                    )
                else:
                    for kind, data in (
                        ("step_started", {}),
                        ("thinking_delta", {"type": "raw_thinking", "text": THINKING}),
                        ("text_delta", {"text": BODY}),
                        *[
                            (
                                "tool_result",
                                {"toolUseId": tool_id, "toolName": name, "input": tool_input, "result": content},
                            )
                            for tool_id, name, tool_input, content in DISPLAY_TOOLS
                        ],
                        ("step_completed", {"durationS": 1}),
                    ):
                        envelope = self.envelope(mode, kind, data)
                        self.journals[mode].append(envelope)
                        self.translators[mode].push(envelope)

    def append_message(self, mode, message):
        session = self.sessions[mode]
        self.manager.storage.append(session.cwd, session.session_id, message)

    def envelope(self, mode, kind, data, *, live=False, run_id=None):
        return {
            "eventType": kind,
            "scope": "step",
            "sequence": len(self.journals[mode].read_all()) + 1,
            "step": {
                "id": "deploy",
                "runId": run_id or ("application-live" if live else "application-history"),
                "index": 1,
                "total": 1,
            },
            "data": data,
        }

    async def publish(self, mode, kind, payload):
        self.events_sent[mode].append(kind)
        await self.sessions[mode].events.publish(kind, payload)

    async def pipeline_event(self, mode, kind, data, *, run_id=None):
        envelope = self.envelope(mode, kind, data, live=True, run_id=run_id)
        self.journals[mode].append(envelope)
        for event in self.translators[mode].push(envelope):
            await self.publish(mode, event["type"], event["payload"])

    async def add_facts(self, mode):
        for product, action, params, body in (
            ("ram", "GetRole", {"RoleName": ROLE}, {"Role": {"RoleName": ROLE}}),
            (
                "ecs",
                "DescribeInstances",
                {},
                {
                    "Instances": {
                        "Instance": [
                            {"InstanceId": INSTANCE, "RegionId": "cn-hangzhou"},
                            {"InstanceId": STREAM_ID, "RegionId": "cn-hangzhou"},
                        ]
                    }
                },
            ),
        ):
            tool_id = "application-facts-" + product
            tool_input = {"product": product, "action": action, "params": params, "region_id": "cn-hangzhou"}
            facts = extract_api_resource_references(
                product=product,
                action=action,
                params=params,
                body=body,
                region_id="cn-hangzhou",
                tool_use_id=tool_id,
                account_scope="fake-application-account",
                console_partition="aliyun",
            )
            assert facts, "The fixture must exercise real API fact extraction"
            metadata = resource_reference_metadata(facts)
            content = json.dumps(body)
            if mode == "normal":
                self.append_message(
                    mode,
                    Message(
                        role="assistant",
                        content=[
                            ToolUseBlock(id=tool_id, name="aliyun_api", input=tool_input),
                        ],
                    ),
                )
                self.append_message(
                    mode,
                    Message(
                        role="user",
                        content=[
                            ToolResultBlock(
                                tool_use_id=tool_id,
                                content=content,
                                metadata={RESOURCE_REFERENCES_METADATA_KEY: metadata},
                            ),
                        ],
                    ),
                )
                await self.publish(
                    mode,
                    "tool.started",
                    {
                        "toolUseId": tool_id,
                        "toolName": "aliyun_api",
                        "input": tool_input,
                    },
                )
                await self.publish(
                    mode,
                    "tool.result",
                    {
                        "toolUseId": tool_id,
                        "content": content,
                        "summary": content,
                        "resourceReferences": resource_fact_to_web(facts),
                    },
                )
                await self.publish(mode, "tool.finished", {"toolUseId": tool_id, "status": "completed"})
            else:
                await self.pipeline_event(
                    mode,
                    "tool_result",
                    {
                        "toolUseId": tool_id,
                        "toolName": "aliyun_api",
                        "input": tool_input,
                        "result": content,
                        "resourceReferences": metadata,
                    },
                )
        await self.sessions[mode].flush_resource_references()

    async def deployment(self, mode, action):
        tool_id = "application-deployment"
        message_id = "application-deployment-message"
        tool_name = "ros_stack" if mode == "normal" else "ros_deploy"
        tool_input = {"action": "CreateStack" if mode == "normal" else "create", "region_id": "cn-shanghai"}

        async def pipeline(kind, data):
            await self.pipeline_event(mode, kind, data, run_id=tool_id)

        if action == "deploy-start":
            if mode == "normal":
                self.append_message(
                    mode,
                    Message(
                        role="assistant",
                        content=[ToolUseBlock(id=tool_id, name=tool_name, input=tool_input)],
                        metadata={"messageId": message_id},
                    ),
                )
                await self.publish(mode, "assistant.message.start", {"messageId": message_id})
                await self.publish(
                    mode,
                    "tool.started",
                    {"toolUseId": tool_id, "toolName": tool_name, "input": tool_input, "messageId": message_id},
                )
            else:
                await pipeline("step_started", {})
                await pipeline("tool_started", {"toolUseId": tool_id, "toolName": tool_name, "input": tool_input})

        identified = action != "deploy-start"
        done = action == "deploy-finish"
        status = "CREATE_COMPLETE" if done else "CREATE_IN_PROGRESS"
        rows = [
            {
                "name": "DeploymentLogicalRole",
                "resource_type": "ALIYUN::RAM::Role",
                "status": status,
                "physical_resource_id": "",
                "attributes": {"RoleName": DEPLOY_ROLE} if identified else {},
            },
            {
                "name": "DeploymentLogicalInstance",
                "resource_type": "ALIYUN::ECS::Instance",
                "status": status,
                "physical_resource_id": DEPLOY_ID if identified else "",
            },
        ]
        progress = {
            "toolUseId": tool_id,
            "stackId": STACK_ID,
            "stackName": "",
            "regionId": "cn-beijing",
            "accountScope": "fake-application-account",
            "consolePartition": "aliyun",
            "status": status,
            "progressPercentage": 100 if done else 50,
            "resources": rows,
            "deploymentComplete": done,
        }
        if mode == "normal":
            await self.publish(mode, "pipeline.event", {"kind": "stack.progress", **progress})
        else:
            await pipeline("stack_progress", progress)
        if done:
            result = {
                "stack_id": STACK_ID,
                "stack_name": "",
                "region_id": "cn-beijing",
                "status": status,
                "is_success": True,
                "resources": rows,
                "resources_fresh": True,
                "outputs": {"RoleName": DEPLOY_ROLE, "InstanceId": DEPLOY_ID},
            }
            # Use the real stack projection with explicit fake execution context.
            facts = _stack_facts(
                {**result, "account_scope": "fake-application-account", "console_partition": "aliyun"}, tool_id
            )
            metadata = {
                STACK_RESULT_METADATA_KEY: result,
                RESOURCE_REFERENCES_METADATA_KEY: resource_reference_metadata(facts),
            }
            content = json.dumps(result)
            if mode == "normal":
                self.append_message(
                    mode,
                    Message(
                        role="user", content=[ToolResultBlock(tool_use_id=tool_id, content=content, metadata=metadata)]
                    ),
                )
                await self.publish(
                    mode,
                    "tool.result",
                    {
                        "toolUseId": tool_id,
                        "content": content,
                        "summary": content,
                        "resourceReferences": resource_fact_to_web(facts),
                    },
                )
                await self.publish(mode, "tool.finished", {"toolUseId": tool_id, "status": "completed"})
                await self.publish(mode, "assistant.message.end", {"messageId": message_id, "finishReason": "tool_use"})
            else:
                await pipeline(
                    "tool_result",
                    {
                        "toolUseId": tool_id,
                        "toolName": tool_name,
                        "input": tool_input,
                        "result": content,
                        "stackResult": result,
                        "resourceReferences": resource_reference_metadata(facts),
                    },
                )
            path = Path(self.sessions[mode].cwd) / ("resource-links-" + mode + ".json")
            path.write_text(TEMPLATE, encoding="utf-8")
            file_input = {"path": str(path), "content": TEMPLATE}
            if mode == "normal":
                self.append_message(
                    mode,
                    Message(
                        role="assistant",
                        content=[ToolUseBlock(id="application-file", name="write_file", input=file_input)],
                    ),
                )
                self.append_message(
                    mode,
                    Message(role="user", content=[ToolResultBlock(tool_use_id="application-file", content="Written")]),
                )
            else:
                await pipeline(
                    "tool_result",
                    {
                        "toolUseId": "application-file",
                        "toolName": "write_file",
                        "input": file_input,
                        "result": "Written",
                    },
                )
                await pipeline("step_completed", {"durationS": 1})
        await self.sessions[mode].flush_resource_references()

    async def add_recovery_facts(self, mode):
        names = recovery_names(mode)
        for label in ("choice", "peer"):
            self.append_message(
                mode,
                Message(
                    role="user",
                    content=f"Application {label}: RAM role {names['role']}",
                    metadata={"messageId": "application-" + label},
                ),
            )
        for kind in ("refresh", "restart"):
            name = names[kind]
            tool_id = "application-query-" + kind
            tool_input = {
                "product": "ros",
                "action": "GetStackResource",
                "region_id": "cn-hangzhou",
                "params": {"StackId": "fake-approval-stack", "LogicalResourceId": name},
            }
            body = {
                "ResourceType": "ALIYUN::FC::Function",
                "PhysicalResourceId": name,
                "LogicalResourceId": name,
                "ResourceAttributes": {"FunctionName": name},
            }
            facts = extract_api_resource_references(
                product="ros",
                action="GetStackResource",
                params=tool_input["params"],
                body=body,
                region_id="cn-hangzhou",
                tool_use_id=tool_id,
                account_scope="fake-application-account",
                console_partition="aliyun",
            )
            metadata = resource_reference_metadata(facts)
            content = json.dumps(body)
            if mode == "normal":
                self.append_message(
                    mode,
                    Message(
                        role="assistant",
                        content=[
                            ToolUseBlock(id=tool_id, name="aliyun_api", input=tool_input),
                        ],
                    ),
                )
                self.append_message(
                    mode,
                    Message(
                        role="user",
                        content=[
                            ToolResultBlock(
                                tool_use_id=tool_id,
                                content=content,
                                metadata={RESOURCE_REFERENCES_METADATA_KEY: metadata},
                            ),
                        ],
                    ),
                )
            else:
                await self.pipeline_event(
                    mode,
                    "tool_result",
                    {
                        "toolUseId": tool_id,
                        "toolName": "aliyun_api",
                        "input": tool_input,
                        "result": content,
                        "resourceReferences": metadata,
                    },
                    run_id="application-query-recovery",
                )
        # Use the actual history API on the next page load, not an injected
        # browser transcript. The fixtures do not create an Agent turn.
        self.sessions[mode].resource_references.hydrate(
            self.manager.load_visible_transcript(
                self.sessions[mode].session_id,
                cwd=self.sessions[mode].cwd,
            )
        )
        await self.sessions[mode].flush_resource_references()

    def snapshot_question(self, mode, action):
        session = self.sessions[mode]
        session.mode = "pipeline"
        self.manager.persist_web_metadata(session)
        A2APersistenceStore(get_config_dir() / "a2a").save_task(
            A2ATaskSnapshot(task_id=session.task_id, context_id=session.context_id, state="input-required")
        )
        prompt = "Snapshot question: RAM role SnapshotOnlyRole; i-application000001."
        if action == "snapshot-question-change":
            prompt = "Revised " + prompt
        event = self.envelope(
            mode,
            "input_received" if action == "snapshot-question-finish" else "input_required",
            {
                "kind": "ask_user_question",
                "inputId": "snapshot-question",
                "toolUseId": "snapshot-question-tool",
                "question": prompt,
                "options": [],
                "allowFreeText": True,
            },
        )
        event.update(
            schemaVersion="1.0",
            eventId=f"snapshot-question-{event['sequence']}",
            taskId=session.task_id,
            contextId=session.context_id,
            pipelineRunId=session.context_id,
            pipelineName=mode,
        )
        self.journals[mode].append(event)
        A2APipelineSnapshotStore(self.journals[mode].pipeline_dir).save(reduce_pipeline_events([event]))

    async def handle(self, request):
        if request.method == "POST":
            payload = await request.json()
            mode, action = payload.get("mode"), payload.get("action")
            if mode not in self.sessions or action not in {
                "facts",
                "stream-start",
                "stream-name",
                "stream-end",
                "handoff",
                "deploy-start",
                "deploy-identifiers",
                "deploy-finish",
                "recovery-facts",
                "auxiliary-start",
                "auxiliary-change",
                "auxiliary-reorder",
                "auxiliary-finish",
                "error-start",
                "error-repeat",
                "error-redraw",
                "error-clear",
                "preview-change",
                "snapshot-question-start",
                "snapshot-question-change",
                "snapshot-question-finish",
            }:
                return JSONResponse({"error": "unknown fixture action"}, status_code=400)
            if action == "facts":
                await self.add_facts(mode)
            elif action == "preview-change":
                path = Path(self.sessions[mode].cwd) / ("resource-links-" + mode + ".json")
                path.write_text(TEMPLATE + "\n", encoding="utf-8")
            elif action.startswith("snapshot-question-"):
                if mode == "normal":
                    return JSONResponse({"error": "pipeline required"}, status_code=400)
                self.snapshot_question(mode, action)
            elif action.startswith("error-"):
                if action == "error-redraw":
                    await self.publish(mode, "session.updated", {"title": self.sessions[mode].title})
                elif action == "error-clear":
                    await self.publish(
                        mode, "user.message", {"turnId": "after-error", "text": "Application next turn."}
                    )
                    await self.publish(mode, "turn.done", {"turnId": "after-error"})
                else:
                    await self.publish(
                        mode,
                        "error",
                        {
                            "message": f"Application failure: RAM role ErrorRole-{mode}; RAM role {ROLE}; {INSTANCE}.",
                            "turnId": "failed-turn",
                            "details": "RAM role HiddenErrorRole",
                        },
                    )
            elif action.startswith("auxiliary-"):
                session = self.sessions[mode]
                if action == "auxiliary-start":
                    self.manager.add_permission_request(
                        session,
                        {
                            "message": f"Auxiliary approval: RAM role {ROLE}; {INSTANCE}.",
                            "command": f"aliyun ram GetRole --RoleName {ROLE}\necho {INSTANCE}",
                        },
                    )
                    for label in ("first", "second"):
                        self.manager.add_question_request(
                            session,
                            {
                                "question": f"Auxiliary {label}: RAM role AuxiliaryRole-{mode}",
                                "allowFreeText": True,
                            },
                        )
                    self.manager.add_elicitation_request(
                        session,
                        {
                            "mode": "form",
                            "message": f"Auxiliary form: RAM role {ROLE}; {INSTANCE}.",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {
                                    key: {
                                        "title": f"Parameter {key}: RAM role {ROLE}; {INSTANCE}.",
                                        "type": "string",
                                        "description": f"Use RAM role AuxiliaryRole-{mode}; {INSTANCE}.",
                                    }
                                    for key in ("first", "second")
                                },
                            },
                        },
                    )
                elif action == "auxiliary-change":
                    pending = next(iter(session.pending_questions.values()))
                    pending.payload["question"] = f"Auxiliary first: revised RAM role AuxiliaryRole-{mode}"
                    await self.publish(mode, "question.request", pending.to_dict())
                elif action == "auxiliary-reorder":
                    pending = next(iter(session.pending_elicitations.values()))
                    pending.payload["fields"].reverse()
                    await self.publish(mode, "elicitation.request", pending.to_dict())
                else:
                    for kind in ("permission", "question", "elicitation"):
                        pending = getattr(session, f"pending_{kind}s")
                        for request_id in list(pending):
                            getattr(self.manager, f"cancel_{kind}_request")(request_id, session_id=session.session_id)
            elif action.startswith("deploy-"):
                await self.deployment(mode, action)
            elif action == "recovery-facts":
                await self.add_recovery_facts(mode)
            elif action == "handoff":
                session = self.sessions[mode]
                session.mode = "normal"
                self.manager.persist_web_metadata(session)
            elif mode == "normal":
                message_id = "application-stream"
                if action == "stream-start":
                    await self.publish(mode, "assistant.message.start", {"messageId": message_id})
                    await self.publish(
                        mode,
                        "assistant.text.delta",
                        {
                            "messageId": message_id,
                            "delta": "Application streaming: i-stream",
                        },
                    )
                elif action == "stream-name":
                    await self.publish(
                        mode,
                        "assistant.text.delta",
                        {
                            "messageId": message_id,
                            "delta": "ing000001; RAM role Applica",
                        },
                    )
                else:
                    content = f"Application streaming: {STREAM_ID}; RAM role {ROLE}."
                    self.append_message(
                        mode, Message(role="assistant", content=content, metadata={"messageId": message_id})
                    )
                    await self.publish(
                        mode,
                        "assistant.text.delta",
                        {
                            "messageId": message_id,
                            "delta": "tionRole.",
                        },
                    )
                    await self.publish(mode, "assistant.message.end", {"messageId": message_id, "finishReason": "stop"})
            else:
                if action == "stream-start":
                    await self.pipeline_event(mode, "step_started", {})
                    await self.pipeline_event(mode, "text_delta", {"text": "Application streaming: i-stream"})
                elif action == "stream-name":
                    await self.pipeline_event(mode, "text_delta", {"text": "ing000001; RAM role Applica"})
                else:
                    await self.pipeline_event(mode, "text_delta", {"text": "tionRole."})
                    await self.pipeline_event(mode, "step_completed", {"durationS": 1})
        return JSONResponse(
            {
                "sessions": {mode: session.web_session_id for mode, session in self.sessions.items()},
                "eventsSent": self.events_sent,
                "recoveryNames": {mode: recovery_names(mode) for mode in self.sessions},
            }
        )

    def route(self):
        return Route("/fixture/resource-application", self.handle, methods=["GET", "POST"])
