---
title: Web App
description: Run IaC Code as a local web app with the same engine as the CLI.
---

# Web App

IaC Code ships with a local web app that runs the same agent engine as the terminal, presented in a browser instead of a REPL. It is useful when you prefer a graphical chat interface, want to manage several conversations side by side, or need to watch pipeline progress and tool activity in a richer layout.

The web app reads and writes the same session store as the CLI, so conversations started in one place can be resumed in the other.

## Installation

The web app is an optional feature that depends on the `http` extra (Starlette and Uvicorn). Install it alongside the base package:

```bash
pip install 'iac-code[http]'
```

If you run `iac-code web` without the extra, the command fails with a message telling you to install `iac-code[http]`. When working from a checkout of the repository, `uv sync --extra http` installs the same dependencies.

## Starting the Web App

Launch the server from the terminal:

```bash
iac-code web
```

By default it binds to `127.0.0.1:8766` and opens your default browser at `http://127.0.0.1:8766`.

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | HTTP server host. Only loopback addresses are accepted. |
| `--port` | `8766` | HTTP server port. |
| `--open` / `--no-open` | `--open` | Open the browser on startup. Use `--no-open` to disable. |

```bash
iac-code web --port 9000 --no-open
```

### Security

The web server binds to loopback interfaces only (`127.0.0.1`, `localhost`, or `::1`). It is intended for use on your own machine and rejects public bind addresses. Do not expose it directly to a network; put it behind your own authenticated proxy if remote access is required.

## Interface Overview

### Sessions Sidebar

The sidebar lists conversations for the selected project. From here you can:

- Start a **New chat**, or switch projects with the project selector.
- **Search** conversations, or open the command palette to run a command.
- **Pin**, **rename**, or **archive** a conversation, and browse archived conversations.

Because sessions are shared with the CLI, a conversation you resume with `iac-code --resume` also appears here. See [Sessions](./cli/sessions.md) for how the session store works.

### Composer

The composer is where you type requests. It exposes the same controls the CLI offers through slash commands and flags:

- **Model and provider** selection for the active session.
- A **Thinking** toggle to enable or disable extended reasoning for supported models.
- A **permission mode** control for how tool actions are approved.
- **Image attachments** for multimodal models.
- **Slash commands** (typed with `/`) and **`@` file references** to point at files in your workspace.

### Normal Chat and Pipeline Mode

A session runs either as a normal chat or in **pipeline** mode. Normal chat streams the assistant's replies, tool calls, and results inline. Pipeline mode adds a workspace that shows step timelines, diagnostics, diagrams, deployment progress, cleanup, and handoff details as the pipeline runs. See [Pipeline Mode](./automation/pipeline-mode.md) for what pipelines do.

The [`selling_solution_first` pipeline](./automation/solution-first-pipeline.md) uses this workspace for a three-stage purchase flow: compare architecture candidates, implement the selected solution, then deploy it after confirmation. Tool approvals appear as localized permission cards under the step that requested them, and unresolved approvals return to the same step when you restore the session.

### Tools and Approvals

Tool calls render as cards in the transcript. When a tool needs your approval, an approval request appears inline; the permission mode set in the composer determines when you are prompted.

### Settings

The settings area collects the same configuration the CLI manages:

- **Cloud credentials** for Alibaba Cloud (see [Alibaba Cloud Credentials](./configuration/alibaba-cloud-credentials.md)).
- **Models** and provider configuration (see [LLM Providers](./configuration/llm-providers.md)).
- **MCP plugins** (see [MCP Integration](./mcp/overview.md)).
- **Memory** inspection and management.

### Interface Language

The web app is available in seven languages — English, 简体中文, 日本語, Français, Deutsch, Español, and Português — selectable from the settings. Your choice is persisted for future sessions.

## Relationship to the CLI

The web app is an alternative front end, not a separate product. It uses the same providers, credentials, skills, tools, and session storage as the terminal. Configure providers and credentials once with `/auth` in the CLI, or through the settings in the web app, and both interfaces share them.
