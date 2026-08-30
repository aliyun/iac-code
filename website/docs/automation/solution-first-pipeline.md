---
title: Solution-first Pipeline
description: Choose an architecture before generating and deploying its ROS template.
---

# Solution-first Pipeline

`selling_solution_first` is an Alibaba Cloud purchasing pipeline that lets you compare architectures before IaC Code generates a ROS template. It implements and prices only the selected solution, reducing work on candidates that will not be deployed.

The existing `selling` pipeline remains available and is still the default. The new pipeline is an explicit alternative; selecting it does not change existing `selling` sessions.

## When to Use It

Use `selling_solution_first` when you want to:

- compare several architectures, products, costs, advantages, and risks before implementation;
- clarify region, scale, networking, availability, or budget before committing to a template;
- generate, preview, and price only the architecture you select;
- review the final ROS parameters and exact quote before creating cloud resources.

| Pipeline | Order of work |
|---|---|
| `selling` | Generate and evaluate candidate templates, choose one, then deploy it. |
| `selling_solution_first` | Plan and choose an architecture, implement only that choice, then deploy it. |

## Start the Pipeline

For the interactive terminal:

```bash
IAC_CODE_MODE=pipeline \
IAC_CODE_PIPELINE_NAME=selling_solution_first \
iac-code
```

For the local Web app, select Pipeline mode when creating a conversation and start the server with the pipeline name:

```bash
IAC_CODE_PIPELINE_NAME=selling_solution_first iac-code web
```

For A2A, a caller can select the mode and pipeline per message instead of changing the server default:

```json
{
  "metadata": {
    "iac_code": {
      "run_mode": "pipeline",
      "pipeline_name": "selling_solution_first",
      "preferredLanguage": "en",
      "candidatePresentation": "rich-v1"
    }
  }
}
```

`pipeline_name` accepts `selling` and `selling_solution_first`. An unsupported non-empty value is rejected instead of silently running another pipeline. Continue a saved pipeline with the same A2A `contextId`; the durable snapshot remains authoritative for the pipeline identity.

## The Three Stages

### 1. Plan and Select a Solution

IaC Code first determines whether the request is a supported Alibaba Cloud infrastructure task. It may ask focused questions when missing information would materially change the product combination, topology, or price.

It then presents one to three comparable solutions. A solution can include:

- an architecture diagram and topology;
- Alibaba Cloud products and resource inventory;
- recommended specifications and hard constraints;
- applicable scenarios and problems solved;
- a rough monthly cost for comparison;
- advantages, disadvantages, risks, and the recommendation rationale.

You can select a solution, ask to adjust the requirement and generate a replacement set, or cancel. No ROS template or cloud resource is created in this stage.

### 2. Implement the Selected Solution

IaC Code works only on the selected solution. It generates and writes the ROS template, validates it, resolves required parameters, runs `PreviewStack`, and requests a precise ROS price estimate.

Before deployment, the interface shows the final architecture, template parameters, and quote. You can:

- confirm deployment;
- change allowed parameters and recalculate;
- return to the first stage and choose or plan another solution;
- cancel without creating cloud resources.

The rough estimate from stage 1 and the precise ROS quote from stage 2 are different values. The deployment confirmation uses the precise quote and the current template parameters.

### 3. Deploy

After confirmation, IaC Code creates the ROS stack, streams authoritative stack progress, waits for the terminal state, and records the stack ID and outputs. Deployment failures remain available for diagnosis and recovery.

## Deployment Confirmation and Tool Permission

Deployment confirmation and tool permission are two separate safety boundaries:

1. **Deployment confirmation** means you accept the selected solution, parameters, and quoted cost.
2. **Tool permission** authorizes the concrete cloud-changing call, such as `ros:CreateStack` or `vpc:CreateVpc`, for this execution.

Approving the first does not automatically approve the second. When a tool requires permission, IaC Code pauses at that point and presents a safe permission request. Read-only, change, and delete operations are distinguished. Cloud API details can include the product, API, region, API call sequence, and redacted parameters; credentials, tokens, signatures, and other sensitive values are never included in display fields.

The user can choose **Allow once** or **Deny**. Permission decisions are correlated to the exact request and written to the permission audit log. An allow decision fails closed if its required audit record cannot be persisted.

## Pause, Recovery, and Handoff

Candidate selection, questions, deployment confirmation, and permission requests are recoverable waits. IaC Code persists the pipeline snapshot before it relies on the caller to continue. After a process restart or conversation reload, the interface reconstructs the completed steps and restores the pending input instead of moving all requests to the end of the conversation.

For A2A integrations:

- `permission_requested` and `permission_resolved` events retain the owning step and candidate coordinates;
- `pendingPermissions` exposes unresolved requests in a restored task snapshot;
- a sideband permission response resumes the original task and context;
- duplicate delivery of the same decision is idempotent, while a conflicting decision is rejected.

When the pipeline completes, fails, exits early, or is canceled, it hands the same context back to normal chat. Follow-up requests can use the selected solution, generated template, deployment result, and cleanup state without starting a new conversation.

## Interfaces and Languages

The pipeline works in the interactive terminal, local Web app, Desktop Web shell, SDK process mode, and A2A server mode. Interface capabilities differ—for example, A2A can request structured `rich-v1` candidate presentation—but the pipeline state and safety boundaries are shared.

User-visible pipeline text supports English, Simplified Chinese, Spanish, French, German, Japanese, and Portuguese. A2A callers select a request language with `metadata.iac_code.preferredLanguage`; protocol field names, enum values, IDs, and JSON shapes are not translated.

## Related Documentation

- [Pipeline Mode](./pipeline-mode.md)
- [Web App](../web-app.md)
- [A2A Protocol Reference](../a2a/protocol-reference.md)
- [Alibaba Cloud Credentials](../configuration/alibaba-cloud-credentials.md)
