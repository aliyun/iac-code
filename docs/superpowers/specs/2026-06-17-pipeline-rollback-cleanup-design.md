# Pipeline Rollback Cleanup Design

## Summary

When the selling pipeline reaches step 5 (`deploying`), it may create an Alibaba Cloud ROS Stack before the step finishes. If the user then rolls back, cancels, or interrupts the pipeline before the `deployment` conclusion is committed, the stack can remain in the cloud without a reliable cleanup record.

This design records stack resources as soon as they are observed, marks only step 5 rollback-related resources as cleanup-required, and starts cleanup after the pipeline hands off to normal chat. Cleanup is executed by the normal AgentLoop, not by a custom cleanup executor. The cleanup prompt is stored in the normal transcript but is not rendered as a visible user prompt in the REPL.

## Goals

- Prevent ROS Stack leakage caused by step 5 rollback in the selling pipeline.
- Persist resource observations and cleanup requirements so crashes do not lose cleanup state.
- Keep pipeline engine generic by moving selling/ROS-specific interpretation into step hooks.
- Use the normal AgentLoop for cleanup, so the user can cancel or continue naturally.
- Let REPL and A2A surfaces show cleanup status without rendering the synthetic cleanup prompt as user input.
- Support both `ros_stack DeleteStack` and `aliyun_api DeleteStack` plus `GetStack` polling, since the model may choose either path.
- Support resume and concurrent A2A sessions without cross-session cleanup state collisions.

## Non-Goals

- Do not block rollback while synchronously deleting resources.
- Do not introduce a custom cleanup executor that directly calls cloud tools.
- Do not build a general cloud CMDB.
- Do not clean resources from normal successful deployments.
- Do not clean all failed or canceled stacks by default; first scope is only step 5 rollback leakage.

## Architecture

The feature is split into four small parts:

1. Resource observation: cloud stack creation emits a resource-observed notification as soon as the stack id is known.
2. Step hooks: the current step hook converts resource notifications into generic descriptors and later decides which observed resources need cleanup after rollback.
3. Cleanup prompt injection: after pipeline handoff to normal chat, pending cleanup resources trigger a synthetic normal AgentLoop turn.
4. Cleanup observer: while the normal AgentLoop runs, a per-session observer listens to tool events and updates cleanup status.

The engine owns persistence and lifecycle wiring. The selling `deploying` hook owns ROS-specific interpretation.

## Persistent Ledger

The cleanup ledger is stored under the pipeline sidecar and written atomically. It is the source of truth for resume, retries, and A2A state.

Example:

```yaml
observed_resources:
  - id: ros-stack/stack-xxx
    source_pipeline: selling
    source_step: deploying
    source_attempt: att_0005
    observed_at: 1781630000.0
    resource:
      provider: ros
      type: stack
      id: stack-xxx
      name: demo-stack
      region_id: cn-hangzhou
    cleanup_required: false

cleanup_resources:
  - id: ros-stack/stack-xxx
    source_observed_id: ros-stack/stack-xxx
    reason: rollback_from_deploying
    status: pending
    cleanup_attempts: 0
    cleanup_run_id: cleanup-0001
    resource:
      provider: ros
      type: stack
      id: stack-xxx
      name: demo-stack
      region_id: cn-hangzhou
    accepted_cleanup_sequences:
      - kind: terminal_tool
        tool: ros_stack
        delete_action: DeleteStack
        success_status: DELETE_COMPLETE
        failure_status: DELETE_FAILED
      - kind: async_api_polling
        delete_tool: aliyun_api
        delete_action: DeleteStack
        status_tool: aliyun_api
        status_action: GetStack
        success_status: DELETE_COMPLETE
        failure_status: DELETE_FAILED
```

Statuses:

- `pending`: cleanup is required but no matching cleanup call has started.
- `running`: a matching cleanup call or polling sequence is in progress.
- `succeeded`: cleanup reached a terminal success state such as `DELETE_COMPLETE`.
- `failed`: cleanup reached a terminal failure state or the tool returned an error.
- `skipped`: the user explicitly chose to keep the resource.

## Resource Observation

Resource observation must happen before final tool result handling. Waiting for `ToolResultEvent` is too late because step 5 can be interrupted or crash while the stack tool is still polling.

For ROS stack creation:

1. `CreateStack` returns `stack_id`.
2. The tool emits a `ResourceObservedEvent` containing action, stack id, stack name, region id, tool use id, step id, and attempt id.
3. The current step hook receives the notification.
4. The hook returns an `ObservedResource` descriptor.
5. The engine persists that descriptor to the ledger.
6. The stack tool continues normal polling.

If the process crashes after the cloud API succeeds but before local persistence, the stack can still be missed. A later enhancement can reduce this window by forcing stack names or tags to include session and attempt identifiers. That enhancement is outside the first implementation scope.

## Step Hook Responsibilities

The selling `deploying` hook gets two new optional hook points:

```python
def on_resource_observed(event, context) -> ObservedResource | None:
    ...

def on_rollback_cleanup_required(context, ledger, rollback) -> list[CleanupResource]:
    ...
```

`on_resource_observed` turns ROS stack creation notifications into generic observed resource descriptors.

`on_rollback_cleanup_required` runs when a rollback leaves `deploying`. It selects only the observed resources created by the relevant `deploying` attempt and returns cleanup descriptors. This keeps the pipeline engine from hardcoding ROS details.

The hook may also provide:

```python
def render_cleanup_prompt(resources) -> str:
    ...
```

If absent, the engine uses a generic prompt built from cleanup descriptors.

## Cleanup Prompt Injection

When the pipeline transitions to normal chat, the normal chat runtime checks the ledger. If any cleanup resource has `pending`, `running`, or `failed` status and is not `skipped`, the runtime injects a synthetic cleanup turn into the normal AgentLoop.

Important behavior:

- The cleanup prompt is stored in the normal transcript.
- The REPL does not render the prompt as a visible user message.
- The REPL renders a separate status line, for example: `Detected 1 leaked rollback resource; starting cleanup.`
- A2A publishes cleanup state events and includes cleanup state in snapshots.
- The cleanup turn uses normal AgentLoop tool execution, permissions, cancellation, and transcript behavior.

Prompt requirements:

- Ask the model to clean only the listed resources.
- Allow `ros_stack DeleteStack`.
- Allow `aliyun_api DeleteStack` followed by `aliyun_api GetStack` polling.
- Require terminal confirmation such as `DELETE_COMPLETE`.
- Forbid creating, updating, or deleting resources outside the list.

## Cleanup Observer

CleanupObserver is a per-session, per-AgentLoop listener. It does not execute tools and does not call `GetStack`. It only observes normal AgentLoop events and updates the ledger.

Startup conditions:

- Immediately after injecting a cleanup prompt.
- On `--resume` when the session ledger contains pending/running/failed cleanup resources.
- On A2A task/context resume with pending/running/failed cleanup resources.
- Before the next normal user turn if cleanup remains unresolved.

The observer is scoped by cwd, session id, task id, context id, pipeline run id, and cleanup run id where available. It must not subscribe to a global event stream without scope filtering.

Event handling:

- `ToolUseEndEvent`: if tool input matches a cleanup delete operation, mark the resource `running` and record `tool_use_id`.
- `StackProgressEvent`: update latest stack status/progress when it can be attributed to a cleanup resource.
- `ToolResultEvent` from `ros_stack`: parse the result. Mark `succeeded` only on `is_success=true` and `status=DELETE_COMPLETE`; mark `failed` on error or `DELETE_FAILED`.
- `ToolResultEvent` from `aliyun_api DeleteStack`: mark delete request observed, but not succeeded.
- `ToolResultEvent` from `aliyun_api GetStack`: parse stack status. Mark `succeeded` on `DELETE_COMPLETE`, `failed` on `DELETE_FAILED`, otherwise keep `running`.

The observer matches resource semantics rather than hardcoding one tool. The descriptor says which operation sequences are accepted.

## REPL UX

The synthetic cleanup prompt is not printed as user text. Instead, REPL displays cleanup state:

- `Detected N leaked rollback resources; starting cleanup.`
- `Cleanup running: <resource name> <status>`
- `Cleanup completed: <resource name>`
- `Cleanup failed: <resource name> <reason>`

Tool calls and stack progress still render normally.

Resume behavior:

- `--resume` reads the ledger and display replay.
- The user can see prior cleanup status messages.
- If cleanup remains unresolved, the next normal chat turn triggers or continues cleanup.

## A2A UX

A2A publishes cleanup-specific metadata events scoped to the current task/context:

- `cleanup_resources_detected`
- `cleanup_started`
- `cleanup_progress`
- `cleanup_completed`
- `cleanup_failed`

Snapshot cleanup shape:

```json
{
  "cleanup": {
    "status": "running",
    "pendingCount": 1,
    "runningCount": 1,
    "failedCount": 0,
    "succeededCount": 0,
    "resources": [
      {
        "id": "ros-stack/stack-xxx",
        "provider": "ros",
        "type": "stack",
        "resourceId": "stack-xxx",
        "regionId": "cn-hangzhou",
        "status": "running",
        "latestStackStatus": "DELETE_IN_PROGRESS"
      }
    ]
  }
}
```

Each event includes task id, context id, pipeline run id, cleanup run id, and resource id so concurrent A2A sessions do not collide.

## Cancellation and Retry

Users can cancel the cleanup turn because cleanup runs through the normal AgentLoop.

If canceled:

- The ledger remains `pending` or `running`.
- The next resume or normal turn rechecks the ledger.
- Cleanup is prompted again unless the user explicitly skips it.

If cleanup fails:

- Status becomes `failed`.
- A later cleanup prompt can retry.
- A simple "continue" means retry cleanup.
- Only an explicit "keep these resources" or "skip cleanup" marks resources `skipped`.

If another session already deleted the stack:

- A missing stack or already-deleted stack should be treated as successful cleanup when the cleanup path can confidently identify the target.

## Concurrency

CleanupObserver is not global. It is attached to the AgentLoop and session being observed.

Ledger updates include enough scope to avoid cross-session writes:

- cwd
- session id
- task id and context id for A2A
- pipeline run id when available
- cleanup run id
- cleanup resource id

If two sessions happen to try deleting the same cloud stack, each session only updates its own ledger. Cloud deletion is treated idempotently where possible.

## Observability

Add observability events for:

- resource observed
- cleanup resource required
- cleanup prompt injected
- cleanup started
- cleanup progress
- cleanup succeeded
- cleanup failed
- cleanup skipped

Attributes should include pipeline name, session id, source step, source attempt, cleanup run id, provider, resource type, resource id, region, status, and error category when available.

## Tests

Unit tests:

- `CreateStack` returns stack id and triggers early resource observation before final tool result.
- `deploying` hook converts resource observations into observed descriptors.
- step 5 rollback converts only matching observed resources into cleanup resources.
- normal successful deployment does not mark cleanup required.
- cleanup prompt is injected when pending cleanup exists.
- cleanup prompt is persisted in transcript but hidden in REPL rendering.
- CleanupObserver marks `ros_stack` `DELETE_COMPLETE` as succeeded.
- CleanupObserver marks `ros_stack` error or `DELETE_FAILED` as failed.
- CleanupObserver handles `aliyun_api DeleteStack` plus `GetStack DELETE_IN_PROGRESS` plus `DELETE_COMPLETE`.
- `--resume` starts observer/injection from ledger state.
- A2A events include scope and snapshot cleanup state.
- concurrent observers do not update each other's ledgers.

No tests may call real Alibaba Cloud APIs.
