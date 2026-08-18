---
sidebar_position: 7
title: Skill Integration
description: External agents drive iac-code through the packaged iac-code Skill and Skill Runtime.
---

# Skill Integration

iac-code ships a packaged Skill for external agents. An external agent (a planner agent or an agent platform) does not install the iac-code Python package and does not invoke headless commands; it drives a local authenticated A2A runtime through a standard-library-only bridge script to run Alibaba Cloud infrastructure work such as ROS/Terraform template generation, cost estimation, resource selection, and deployment.

## Components

| Component | Location | Description |
|---|---|---|
| Skill package | `skills/iac-code/` | `SKILL.md` instructions, `agents/` agent metadata, and `scripts/iac_code.py`, the bridge script |
| Skill Runtime | Published per platform | CPython 3.12 native executable embedding the iac-code A2A server |
| Distribution contracts | `skill-runtime/skill-package-contract.json`, `skill-runtime/publisher-contract.json` | Format and verification constraints for skill packages and publishers |

The bridge script is written entirely with the Python standard library and stays compatible with Python 3.8+; CI compiles and smoke-runs it on the full 3.8–3.14 matrix. Do not add third-party dependencies or newer-only syntax to the bridge.

## Runtime Acquisition and Cache

On first use the bridge reads the manifest, downloads the artifact for the current platform, verifies its size and SHA-256, installs it, and caches it under `<IAC_CODE_CONFIG_DIR or ~/.iac-code>/skill-runtime/<runtime-tag>/<target>/`.

- `python3 scripts/iac_code.py ensure-runtime` — prepare the runtime ahead of time; a cached runtime is reused.
- `python3 scripts/iac_code.py cache list` — show installed runtimes and candidate packages.
- `python3 scripts/iac_code.py cache clean [--runtime-tag <tag>] [--candidates] --confirm` — clean runtime caches or candidate packages; requires explicit `--confirm`.

## Configuration Preflight

Before creating a job, `start` runs a configuration readiness check through the runtime. The preflight does not read secret values; it only reports readiness:

| Situation | Result |
|---|---|
| LLM provider or API key incomplete | Returns `llm_not_configured` and refuses to create the job |
| Selling pipeline with incomplete Alibaba Cloud credentials | Returns `cloud_credentials_not_configured` and refuses to create the job |
| Normal mode with incomplete Alibaba Cloud credentials | May continue for work that does not call cloud APIs, with a preflight warning |

## Command Reference

| Command | Purpose |
|---|---|
| `start` | Create a job: `--mode normal|pipeline`, `--pipeline-name`, `--cwd` absolute workspace, `--prompt-file` UTF-8 prompt file, `--language auto|en|zh|es|fr|de|ja|pt`, optional `--follow` |
| `follow` | Consume the event stream until the next interaction boundary: `--job-id`, `--cursor`, `--wait-seconds` (default 60s, maximum 120s) |
| `continue` | Continue a normal-mode conversation in the same job: `--job-id`, `--prompt-file`, optional `--follow` |
| `respond` | Answer a pending input, see [User input](#input-required) |
| `poll` | One-shot polling for diagnosis and recovery only; do not use it as a `follow` replacement |
| `cancel` | Cancel the job |
| `ensure-runtime` / `cache list` / `cache clean` | Runtime and cache management |

`start --follow` and `follow` write step boundaries and low-frequency heartbeats to stderr; stdout carries exactly one bounded JSON result.

## Interaction Boundaries {#boundaries}

`--follow` consumes the event stream until the next step boundary, permission request, user question, candidate selection, `turn_completed`, or terminal state. A boundary result carries:

- `boundaryReached: true` — a boundary was reached; this does **not** mean the job is complete;
- `presentationRequired: true` and `userUpdates` — localized strings ready to display to the user;
- the `cursor` needed to continue.

The external agent must first present every received `userUpdates` string in a user-visible reply, then immediately call `follow` again with the returned `cursor`. Do not answer the infrastructure task in parallel or raise unrelated questions while a follow is running.

## User Input {#input-required}

A result contains `inputRequired` when user input is needed. There are three kinds:

- `permission` — a tool or deployment permission request. The envelope contains `inputId`, `toolUseId`, a title, purpose, effect, target, read-only flag, `safeSummary`, and, for deployment requests, `deploymentSummary`. The external agent should decide according to its own permission policy: if the same operation would proceed without asking when the agent runs it directly, answer `allow_once`; if its policy would deny, answer `deny`; otherwise ask the user. iac-code's own denials must not be overridden.
- `ask_user_question` — a multiple-choice or free-text question. Present the prompt and options as-is; accept free text only when `allowFreeText` is `true`.
- `candidate_selection` — pipeline plan selection. Present each candidate's summary, architecture diagram (Mermaid), total monthly cost, and cost items first, then return the selected candidate. Never replace the provided prices with rough estimates.

`respond` has two forms:

```bash
# Inline decision for permissions
python3 scripts/iac_code.py respond --job-id <job-id> \
  --input-id <inputId> --tool-use-id <toolUseId> --decision allow_once --follow

# Questions and candidate selections use an answer file
python3 scripts/iac_code.py respond --job-id <job-id> --input-file <answer.json> --follow
```

An answer must preserve every correlation field of the pending input and stays bound to the current `kind`, `inputId`, `requestTaskId`, and `contextId`; never reuse an answer from another request, and never reinterpret a resource selection as a deployment confirmation.

## Language Control

`start --language` sets the job's preferred language (use `auto` when unknown). Every result of that job repeats `preferredLanguage`; treat it as durable control state: progress, questions, permission prompts, candidate plans, and final results are presented in that language, while protocol field names, enums, IDs, and commands stay unchanged. When authoritative text already uses that language, present it directly or summarize it in the same language; never translate Chinese user-visible content into English.

## Relationship with the A2A Protocol

The bridge talks to the local runtime over HTTP A2A JSON-RPC; task states, artifacts, and permission interactions reuse the iac-code A2A protocol:

- Permission sideband responses use the `schemaVersion 1` message format; see [Protocol reference](./protocol-reference.md) for fields and constraints.
- In pipeline mode, passing `candidatePresentation: rich-v1` returns structured candidate presentation payloads.
- Job result states map to A2A task states: `turn_completed` finishes a normal turn; pipeline terminal states are `completed`, `failed`, `canceled`, and `rejected`, with `pipelineResult` and `artifacts` as the authoritative result.

## Security Boundary

- The runtime listens only on a random port on `127.0.0.1`; every startup generates a fresh random Bearer token, and every bridge request carries it.
- The bridge keeps artifacts and results inside the job workspace; results are written to `.iac-code-skill-results/` in the workspace.
- Preflight reports and permission display fields are sanitized; secrets and credentials never appear in display fields.
