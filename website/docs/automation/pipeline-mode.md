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

Pipeline mode currently requires the interactive REPL. It cannot be combined with `--prompt`.

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

## Automation Integrations

Pipeline mode is currently primarily designed for the interactive REPL. A2A server mode can expose pipeline progress, artifacts, permission results, and recovery information, which is useful when connecting a pipeline to an external console or task system.

ACP does not currently support Pipeline mode. `--prompt` / [Non-interactive Mode](./non-interactive-mode.md) runs a normal one-shot request and does not execute Pipeline steps.

## Current Limitations

- The current release includes only the `selling` pipeline, mainly for Alibaba Cloud infrastructure workflows.
- Pipeline mode requires the interactive REPL. `--prompt` is rejected when `IAC_CODE_MODE=pipeline`.
- Pipeline mode supports text input. Images pasted into the REPL are ignored while the pipeline is active.
- Mid-pipeline shell escapes, skill triggers, and most slash commands are restricted unless the pipeline definition explicitly allows them. Basic commands such as `/help`, `/status`, `/resume`, and `/exit` remain available.
