---
sidebar_position: 3
title: IaC Code Skill Host Integration Reference
description: Integrate the packaged IaC Code Skill bridge with a Skill-capable host agent.
---

# IaC Code Skill Host Integration Reference

This document is for developers of agents and Skill distribution systems. It defines how a host invokes the packaged
bridge, presents IaC Code results, handles user interaction, and resumes an existing task. End users should read
[Install and Use the IaC Code Skill](./skill-integration.md).

## Integration Model

The Skill package contains `SKILL.md` and the standard-library-only `scripts/iac_code.py` bridge. The host invokes the
bridge; the bridge installs and starts the pinned, verified Runtime and communicates with it over an authenticated
local A2A connection.

The host must:

- use CPython 3.8–3.14 to run the bridge;
- treat stdout as the stable JSON result and stderr as diagnostics and bounded progress;
- preserve the current `jobId`, `contextId`, cursor, and input correlation fields;
- show every user-facing boundary before continuing; and
- fail closed on bridge errors instead of bypassing the bridge with direct cloud calls or another Runtime.

## Optional Distribution Configuration

A distributor can place `config.json` beside `SKILL.md`:

```json
{
  "channel": "codex",
  "pipelineName": "selling_solution_first",
  "permissionWaitPolicy": {
    "residentTimeoutSeconds": null,
    "subPipelineTimeoutSeconds": null,
    "timeoutGraceSeconds": 30
  }
}
```

- `channel` is the channel identifier; the bridge adds the `skill/` prefix.
- `pipelineName` applies only after Pipeline mode is selected. The default is `selling_solution_first`; `selling` is
  available for distributors that explicitly require the legacy workflow.
- `permissionWaitPolicy` controls waits in the temporary A2A server owned by the Skill. `null` means unlimited for the
  resident or Sub Pipeline timeout.

The bridge rejects unknown fields and invalid values. This file is installation policy: do not derive it from a user
request, expose it in task output, or modify it during a task.

## Start a Job

Write the complete request to a UTF-8 file in the workspace, resolve the workspace to an absolute path, and run:

```text
python3 scripts/iac_code.py start --mode normal --cwd <workspace> --prompt-file <prompt-file> --language <language> --follow
```

Use `normal` by default. Select `pipeline` only for a requested solution-comparison flow that needs candidate
architectures, cost comparison, confirmation, and deployment. Set the language to `en`, `zh`, `es`, `fr`, `de`, `ja`,
`pt`, or `auto`. Keep the returned `preferredLanguage` for every later turn.

`start` performs a non-secret readiness check. `llm_not_configured` stops before job creation. Pipeline mode also
requires cloud credentials and otherwise returns `cloud_credentials_not_configured`. Normal mode may proceed with a
warning when the task does not need cloud APIs.

## Follow Progress and Completion

`--follow` stops at the next presentation or interaction boundary, `turn_completed`, or terminal Pipeline state. When
a result has `boundaryReached: true`, show all strings in `userUpdates`, then immediately follow the same job using the
returned cursor:

```text
python3 scripts/iac_code.py follow --job-id <job-id> --cursor <cursor> --wait-seconds 60
```

Do not treat `boundaryReached` as completion. `presentationRequired` means that the current update must be made visible
before another bridge call. A normal-mode answer is authoritative only when `state` is `turn_completed`; use
`finalText` and `artifacts`. For a terminal Pipeline state, use `pipelineResult` and `artifacts` and report cleanup
failures instead of claiming success.

If `follow` cannot be used during diagnosis or recovery, poll the same job:

```text
python3 scripts/iac_code.py poll --job-id <job-id> --cursor <cursor> --wait-seconds 5
```

When a result says `state: input-required` but does not contain `inputRequired`, report its latest text or error and
leave the job unchanged. Do not submit a duplicate response or create a replacement job.

## Handle User Input

Treat every `inputRequired` object as a hard interaction boundary. Present it through the host's native question or
approval UI, stop, and wait for an explicit answer. Never infer an answer from the original request or choose a
default. Preserve `kind`, `inputId`, `requestTaskId`, `contextId`, and `toolUseId` when present.

| `kind` | What the host must present | Response |
|---|---|---|
| `permission` | Purpose, effect, target, read-only status, deployment summary, safe summary, and returned actions | `allow_once` or `deny` |
| `ask_user_question` | The prompt, options, and free-text prompt when allowed | Selected option or allowed free text |
| `candidate_selection` | Every summary, Mermaid architecture diagram, monthly total, and cost items | Candidate ID or index |
| `deployment_confirmation` | Solution, template URL, quote or quote failure, effective parameters, overrides, Preview status, and returned actions | `confirm`, `adjust`, `reselect`, or `cancel` |

Write the correlated answer to a new UTF-8 JSON file and resume the same job:

```text
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer-file> --follow
```

Example envelopes:

```json
{"kind":"permission","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","toolUseId":"<toolUseId>","decision":"allow_once"}
```

```json
{"kind":"ask_user_question","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<answer>"}
```

```json
{"kind":"candidate_selection","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","answer":"<candidate ID or index>"}
```

```json
{"kind":"deployment_confirmation","requestTaskId":"<requestTaskId>","contextId":"<contextId>","inputId":"<inputId>","action":"<confirm|adjust|reselect|cancel>","parameterOverrides":{"<parameter>":"<value>"}}
```

Omit `parameterOverrides` when the user did not request an adjustment. A deployment request is not approval for a
later `deployment_confirmation`, and an outer host approval must not override a denial from IaC Code.

## Continue a Conversation

After a normal turn completes, or after a completed Pipeline hands the conversation to normal mode, write the next
message to a new prompt file and continue the existing job:

```text
python3 scripts/iac_code.py continue --job-id <job-id> --prompt-file <prompt-file> --follow
```

Keep the same `jobId` and `contextId`; a new `taskId` for each normal turn is expected. Do not use `start` merely
because the previous turn completed. Keeping the job identity also allows the bridge to recover permission waits and
resume after a host interruption.

To cancel the whole operation, run:

```text
python3 scripts/iac_code.py cancel --job-id <job-id>
```

Cancellation is different from denying one permission request.

## Errors and Runtime Lifecycle

Treat a pre-job bridge error as authoritative. In particular, `incompatible_host` includes available host and Runtime
compatibility facts; present them and stop. Do not fall back to pip installation, another Runtime artifact, or direct
cloud calls.

The downloaded Runtime is cached under
`<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`. The package layout and integrity metadata
are defined by `skill-runtime/skill-package-contract.json` and the release manifest. The bridge verifies the package
before use. Runtime cache cleanup must be a separate, explicitly requested operation; current and active packages are
protected.

The Runtime binds to a random `127.0.0.1` port and generates a process-specific Bearer token. Do not expose the token,
local state, credentials, environment values, or raw tool inputs and results. Bounded result projections and display
fields are the supported host interface.

## Related Documentation

- [Official IaC Code Skills](./skill-overview.md)
- [Install and Use the IaC Code Skill](./skill-integration.md)
- [A2A Protocol Overview](./overview.md)
- [A2A Protocol Reference](./protocol-reference.md)
- [Runtime Configuration](../configuration/runtime-configuration.md)
