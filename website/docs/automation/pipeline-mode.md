---
title: Pipeline Mode
description: Use step-by-step Pipeline mode to guide complex infrastructure tasks.
---

# Pipeline Mode

Pipeline mode is an interactive mode that runs work step by step. It is useful for infrastructure tasks that are longer or easier to get wrong than a normal chat request: understand the requirement, plan an approach, generate artifacts, ask the user to confirm, and then continue with the next actions.

Pipeline itself is a general capability. The built-in implementation available today is the `selling` pipeline. `selling` targets Alibaba Cloud infrastructure scenarios and can take a deployment request through candidate architectures, ROS templates, cost estimates, and deployment after confirmation.

Good requests for Pipeline mode include:

```text
Select an existing VPC and create a VSwitch
```

```text
Design a low-cost Alibaba Cloud web application deployment and generate a template
```

## Start Pipeline Mode

Pipeline mode can run through the interactive REPL or through SDK process mode. It cannot be combined with `--prompt`.

On macOS or Linux:

```bash
IAC_CODE_MODE=pipeline iac-code
```

On PowerShell:

```powershell
$env:IAC_CODE_MODE = "pipeline"
iac-code
```

The default pipeline name is `selling`. To be explicit:

```bash
IAC_CODE_MODE=pipeline IAC_CODE_PIPELINE_NAME=selling iac-code
```

For SDK subprocess clients, start process mode with stream-json input and output:

```bash
IAC_CODE_MODE=pipeline iac-code --input-format stream-json --output-format stream-json
```

## Pipeline and selling

| Name | Meaning |
|---|---|
| Pipeline mode | IaC Code's general step-by-step execution mode for long flows, confirmation points, recovery, and progress display. |
| `selling` pipeline | The current built-in pipeline for Alibaba Cloud infrastructure design, template generation, cost estimation, and deployment. |

If more pipelines are added later, select them with `IAC_CODE_PIPELINE_NAME`. The current release includes `selling`.

## Environment Variables

| Variable | Purpose |
|---|---|
| `IAC_CODE_MODE=pipeline` | Enables Pipeline mode. Any other value falls back to normal mode. |
| `IAC_CODE_PIPELINE_NAME` | Selects the pipeline definition. The default is `selling`. |
| `IAC_CODE_CWD` | Overrides the working directory used by the pipeline. |
| `IAC_CODE_PIPELINE_SELLING_ENABLE_REVIEWING` | Enables the optional template review step in the `selling` pipeline. |

## What happens in the selling pipeline

The `selling` pipeline breaks an infrastructure request into user-visible stages:

| Stage | What you see |
|---|---|
| Understand the requirement | IaC Code checks whether the request is an Alibaba Cloud infrastructure task. If important details are missing, it asks before generating a plan. |
| Plan architectures | IaC Code proposes one or more candidate architectures so you can compare tradeoffs. |
| Generate and evaluate | IaC Code generates ROS templates for candidate plans and estimates resource costs. |
| Confirm a plan | IaC Code shows candidate details and waits for you to choose the plan to continue with. |
| Deploy | After a plan is selected, IaC Code enters the deployment stage and handles tools or higher-risk operations according to the permission policy. |

If you mention constraints such as "use an existing VPC" or "do not create this resource type", the `selling` pipeline will try to respect them in later plans and templates. You do not need to know the internal fields; just write the constraints in the request.

## Interaction and Recovery

Pipeline mode may pause and wait for user input, for example:

- The requirement is unclear and IaC Code needs the target, scale, region, or budget.
- There are multiple candidate plans and you need to choose one.
- A tool or deployment action requires permission approval.
- The run was interrupted and needs to be resumed or continued.

If the process exits or the session is interrupted, IaC Code saves the pipeline state. When you later return to the session with `--resume`, you can inspect the previous progress and continue from a recoverable point.

After the pipeline completes, fails, exits early, or is canceled, IaC Code switches back to normal chat. You can then ask follow-up questions, adjust the plan, or handle post-deployment issues.

Switching back to normal chat also requires the pipeline's required results. If a required step has not produced a usable result yet — for example the run is canceled while the requirement-understanding stage is still working — IaC Code publishes only the terminal state and does not signal handoff readiness, so no downstream consumer receives an empty result as if the pipeline had finished its work.

## Automation Integrations

Pipeline mode can be integrated through A2A server mode or SDK process mode. A2A server mode exposes pipeline progress, artifacts, permission results, and recovery information for external consoles or task systems. SDK process mode keeps `iac-code` as a local subprocess and exchanges line-delimited JSON over stdin/stdout.

When integrating the pipeline over A2A, callers can also declare two capabilities in message metadata: `metadata.iac_code.preferredLanguage` returns progress, questions, and permission prompts in the caller's preferred language; `metadata.iac_code.candidatePresentation: rich-v1` makes the plan confirmation step return a structured payload (candidate name, summary, architecture diagram, total monthly cost, cost items) suitable for rich rendering in external interfaces. When a deployment or tool operation requires permission, the pipeline publishes a pending permission envelope and callers answer with `allow_once` or `deny` through a sideband message; see the [Protocol reference](../a2a/protocol-reference.md) for details.

In SDK process mode, pipeline events are emitted as `stream_event` frames whose event payload has `type: "pipeline_event"`. Final `result` frames include a `pipeline` object with `contextId`, `taskId`, `iacCodeSessionId`, `status`, and `sidecarStatus`. A paused pipeline should be resumed by sending the same `contextId` and active `taskId`. If a context has a recoverable task and the client omits the task id, the process returns a retryable `pipeline_task_required` error with `recoverableTaskId`.

ACP does not currently support Pipeline mode. `--prompt` / [Non-interactive Mode](./non-interactive-mode.md) runs a normal one-shot request and does not execute Pipeline steps.

## Current Limitations

- The current release includes only the `selling` pipeline, mainly for Alibaba Cloud infrastructure workflows.
- Pipeline mode supports the interactive REPL and SDK process mode. `--prompt` is rejected when `IAC_CODE_MODE=pipeline`.
- Pipeline mode supports text input. Images pasted into the REPL are ignored while the pipeline is active.
- Mid-pipeline shell escapes, skill triggers, and most slash commands are restricted unless the pipeline definition explicitly allows them. Basic commands such as `/help`, `/status`, `/resume`, and `/exit` remain available.


## Backup Checkpoints

Pipeline mode does not back up completed agent-loop steps. It publishes `input_required` or `waiting_input` first and then runs one critical backup; if that backup fails, the pipeline follows with `backup_blocked` and pauses in a recoverable state. `pipeline_handoff_ready` and terminal states remain protected by a critical backup before publication. For A2A observers, terminal and `pipeline_handoff_ready` protected publications are followed by a `backup_committed` event with `committedEventId`, `committedEventType`, and `committedSequence` once the current backup boundary is durable. With `IAC_CODE_CONFIG_BACKUP_TMP_DIR`, that boundary is the local immutable snapshot and supports recovery in the same sandbox; cross-sandbox recovery depends on the background process copying it to `IAC_CODE_CONFIG_BACKUP_DIR`. Without staging, the boundary remains the final backup directory. `parallel_sub_pipeline` child-step progress is captured by the next waiting, handoff, or terminal backup rather than by per-step checkpoints.
