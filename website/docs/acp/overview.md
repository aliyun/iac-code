---
sidebar_position: 1
title: ACP Protocol
description: Overview of Agent Client Protocol support in iac-code.
---

# ACP Protocol

## What is ACP

[Agent Client Protocol (ACP)](<https://agentclientprotocol.com/get-started/introduction)>) is a standardized communication protocol between AI agents and their clients. It defines how clients (IDEs, editors, automation tools) start, interact with, and manage agent sessions over structured JSON-RPC messages.

## iac-code as an ACP Server

iac-code exposes its Infrastructure as Code capabilities through an ACP server. Any ACP-compatible client can launch `iac-code acp` as a subprocess (or connect over HTTP+SSE) and programmatically:

- Create sessions scoped to a project directory
- Send natural-language prompts and receive streaming responses
- Approve or reject file-write and destructive operations
- Manage multiple concurrent sessions

This turns iac-code from a terminal tool into a **composable backend** for any development environment.

## Use Cases

- **IDE / Editor integration** — Zed, VS Code, or custom editors can embed iac-code as a context server to provide IaC generation inline.
- **Agent-to-Agent orchestration** — Other AI agents can call iac-code's IaC capabilities via the protocol, enabling multi-agent workflows.
- **Automation pipelines** — CI/CD scripts or chatops bots can invoke iac-code headlessly to generate and validate templates.

## Interaction Modes Comparison

| Mode | Command | Best For |
|------|---------|----------|
| **Interactive REPL** | `iac-code` | Hands-on exploration, iterative template authoring |
| **Non-interactive CLI** | `iac-code --prompt "..."` or `--headless` | Scripting, one-shot generation, CI pipelines |
| **ACP Server** | `iac-code acp` | IDE integration, multi-session management, programmatic access |

The ACP Server mode is the only mode that supports multiple concurrent sessions and provides structured streaming events (tool calls, permission requests, thinking) rather than plain text output.

## Core Capabilities

- **Multi-session management** — Create, list, fork, resume, and close independent sessions, each with its own conversation history and working directory.
- **Streaming responses** — Real-time events for agent text, thinking, tool calls, tool progress, and completion.
- **Permission framework** — Read-only tools auto-allow; write and destructive tools require explicit client approval before execution.
- **Dual transport** — Stdio for local/subprocess usage, HTTP+SSE for remote and network scenarios.
- **MCP server configuration passthrough** — Clients can declare MCP servers at session creation for tool augmentation.
- **Slash commands support** — Forward slash commands (`/compact`, `/clear`, `/debug`, etc.) through the protocol.
- **Runtime metrics** — Session-level token usage, latency, and tool call statistics.
