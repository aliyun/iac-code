---
title: Web App
description: Start the local Web app and use its settings, conversations, and composer.
---

# Web App

Run `iac-code` in a terminal to start the local Web app and open your browser. On first use, configure the LLM provider and Alibaba Cloud credentials in Settings.

Desktop and Web share saved conversations and settings. Conversations created through other interfaces may be visible only in read-only mode; see [Sessions](./cli/sessions.md) for visibility and continuation.

## Installation

Web and HTTP runtime dependencies are included in the default installation. The `[http]` extra remains a compatibility alias; a separate installation is no longer required. From a checkout, `uv sync` is sufficient.

```bash
pip install iac-code
```

## Starting the Web App

Launch the server from the terminal:

```bash
iac-code web
```

By default it binds to `127.0.0.1:8766` and opens your default browser at `http://127.0.0.1:8766`.

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | HTTP server host. Local mode accepts loopback addresses; other IP addresses require `--access-token-file`. |
| `--port` | `8766` | HTTP server port. |
| `--open` / `--no-open` | `--open` | Open the browser on startup. Use `--no-open` to disable. |
| `--debug`, `-d` | `false` | Enable server debug logging for this run. |
| `--access-token-file <path>` | — | Token file for explicit remote access with encrypted API transport. |

```bash
iac-code web --port 9000 --no-open
```

### Security

Without `--access-token-file`, the server accepts only loopback hosts (`127.0.0.1`, `localhost`, or `::1`) and checks local requests. Explicit token mode also accepts other IP addresses and protects API requests with the encrypted token transport.

## Deploy an existing ROS template

1. Configure [cloud credentials](./configuration/alibaba-cloud-credentials.md), then open a conversation in the project containing the template.

2. Type `@`, select the template file, and ask the assistant to inspect it and list the parameters needed for deployment.

3. Provide the target region, stack name, and parameter values in the conversation. Ask for validation before creating the stack.

4. Review the validation result and proposed changes. When ready, ask the assistant to create the stack.

5. If a cloud API approval card appears, check the operation and approve or reject it. An approved create operation can create billable cloud resources.

6. Read the stack ID, progress, and final result in the conversation. On failure, ask the assistant to diagnose the reported error in the same conversation.

## Interface Overview

### Sessions Sidebar

The sidebar groups saved conversations by project. From here you can:

- Start a **New chat**; choose its project below the composer.
- **Search** conversations, or open the command palette to run a command.
- **Pin**, **rename**, or **archive** a conversation, and browse archived conversations.

Web, Desktop, and headless share saved conversations. See [Sessions](./cli/sessions.md) for resuming and renaming them.

### Composer

Use the composer and its controls to describe a task and choose how the conversation runs:

- **Model and provider** selection for the active session.
- A **Thinking** toggle to enable or disable extended reasoning for supported models.
- A **permission mode** control for how tool actions are approved.
- **Image attachments** for multimodal models.
- **Skills** selected from `/` or `$` suggestions, and **`@` workspace file references**.

### Normal Chat and Pipeline Mode

Choose the mode and, for Pipeline mode, the pipeline when creating a conversation. Conversations in the same app can use different modes without restarting the server.

A session runs either as a normal chat or in **pipeline** mode. Normal chat streams the assistant's replies, tool calls, and results inline. Pipeline mode adds a workspace that shows step timelines, diagnostics, diagrams, deployment progress, cleanup, and handoff details as the pipeline runs. See [Pipeline Mode](./automation/pipeline-mode.md) for what pipelines do.

The [`selling_solution_first` pipeline](./automation/solution-first-pipeline.md) uses this workspace for a three-stage purchase flow: compare architecture candidates, implement the selected solution, then deploy it after confirmation. Tool approvals appear as localized permission cards under the step that requested them, and unresolved approvals return to the same step when you restore the session.

### Tools and Approvals

Tool calls render as cards in the transcript. When a tool needs your approval, an approval request appears inline; the permission mode set in the composer determines when you are prompted.

### Settings

Desktop and Web share the same interface. Use Settings and the visible conversation controls for everyday work.

- **Cloud credentials** for Alibaba Cloud (see [Alibaba Cloud Credentials](./configuration/alibaba-cloud-credentials.md)).
- **Models** and provider configuration (see [LLM Providers](./configuration/llm-providers.md)).
- **MCP plugins** (see [MCP Integration](./mcp/overview.md)).
- **Memory** inspection and management.

### Interface Language

The web app is available in seven languages — English, 简体中文, 日本語, Français, Deutsch, Español, and Português — selectable from the settings. Your choice is persisted for future sessions.

## Relationship to the CLI

Web, Desktop, and headless use the same providers, credentials, skills, tools, and session store. Configure providers and cloud access in Settings once, then reuse them across these entry points.
