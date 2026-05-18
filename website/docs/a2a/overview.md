---
sidebar_position: 1
title: A2A Protocol
description: Overview of Agent2Agent support in iac-code.
---

# A2A Protocol

## What is A2A

[Agent2Agent (A2A)](https://github.com/a2aproject/A2A) is a protocol for discovering and calling remote agents. It lets an agent publish an Agent Card, accept structured messages, stream task updates, and expose cancellation and task lookup operations through standard transports.

## iac-code as an A2A Server

iac-code can run as an A2A 1.0 Server / Agent. Other A2A-compatible clients can discover it, send Infrastructure as Code requests, stream execution updates, and cancel active tasks.

Use A2A when another agent, workflow engine, or service needs to call iac-code as an interoperable IaC specialist. Use ACP when an editor-style client needs session management, permission prompts, and local development integration.

## Use Cases

- **Agent orchestration** — A planner agent can delegate Alibaba Cloud ROS or Terraform work to iac-code.
- **Workflow automation** — Internal tools can submit IaC generation, review, or conversion tasks over HTTP.
- **Service discovery** — Clients can fetch the Agent Card and choose capabilities such as IaC generation or template review.
- **Streaming integrations** — A chatops or dashboard client can show model text, tool activity, usage metadata, and final task state as the turn runs.

## Interaction Modes Comparison

| Mode | Command | Best For |
|------|---------|----------|
| **Interactive REPL** | `iac-code` | Hands-on exploration and iterative template authoring |
| **Non-interactive CLI** | `iac-code --prompt "..."` or `--headless` | One-shot scripting and CI jobs |
| **ACP Server** | `iac-code acp` | IDE/editor integration and multi-session client control |
| **A2A Server** | `iac-code a2a` | Agent-to-agent interoperability over A2A transports |
| **A2A Client** | `iac-code a2a-client call` | Calling remote A2A agents from iac-code |

## Core Capabilities

- **Agent Card discovery** — Publishes `/.well-known/agent-card.json` with protocol binding, version, skills, input/output modes, and optional auth metadata.
- **HTTP JSON-RPC and REST** — Serves A2A JSON-RPC requests at `/` and registers the SDK REST routes.
- **Streaming responses** — Supports `SendStreamingMessage` for incremental task updates.
- **Task management** — Supports task lookup, authenticated task listing with cursor pagination, active task cancellation, and active task subscription.
- **Context reuse** — Reuses an iac-code runtime for follow-up messages in the same A2A `contextId`.
- **Workspace scoping** — Reads the project directory from message metadata at `iac_code.cwd`.
- **Tool metadata** — Emits iac-code-specific metadata for tool starts, input deltas, completed tool results, permission decisions, and token usage.
- **Input parts** — Accepts text-like parts, JSON data parts, raw UTF-8 text, local workspace `file://` text files, and bounded multimodal attachments represented as prompt manifests.
- **Client calls** — Discovers remote Agent Cards, verifies signatures when configured, and sends text prompts to remote agents.
- **Routing** — Selects configured remote agents by explicit name, skill, or prompt/tag matching.
- **Persistence metadata** — Mirrors local A2A task/context snapshots to JSON files for cross-process restoration metadata.
- **Artifacts** — Stores supported local text artifact payloads outside the streamed event body, emits standard `TaskArtifactUpdateEvent` events, and records task `artifacts`.
- **Extensions and caching** — Advertises the optional iac-code artifact metadata extension, validates required `A2A-Extensions`, and serves Agent Cards with cache headers.
- **Push notifications** — Supports A2A task push notification config methods when `push-notifications: true` is configured, with local-file or Redis-backed delivery queues.
- **Agent Card signing** — Adds optional A2A SDK JWS signatures for Agent Cards and supports `kid`-based verification with configured keys, local octet JWKS data, or a remote JWKS URL.
- **Multiple transports** — Runs over HTTP, stdio, Unix sockets, WebSocket, official gRPC, custom gRPC JSON-RPC, and Redis Streams transports.
- **CLI operations** — Provides commands for discovery, message sending, task lookup/list/cancel/subscribe, push config CRUD, extended cards, and route previews.

## Phase 1 Support

iac-code supports A2A server mode over HTTP JSON-RPC/REST and several optional transports, plus Phase 1 client mode for calling remote A2A agents. It can discover remote Agent Cards, select advertised endpoints, send A2A 1.0 prompts, query/list/cancel/subscribe to tasks, route to configured agents, persist local task/context restoration metadata, store local artifact payloads as standard task artifacts, validate required extensions, manage push notification configs, and sign or verify Agent Cards with HMAC or JWKS metadata.

## Phase 1 Unsupported

- stdio, Unix sockets, WebSocket, gRPC JSON-RPC envelope, and Redis Streams are experimental custom JSON-RPC transports.
- Official gRPC requires optional dependencies and uses an insecure local server binding by default.
- No distributed or shared task store. Persistence is local file storage under the iac-code runtime configuration area.
- No restoration of an in-flight asyncio task after process restart.
- No automatic background continuation of interrupted remote tasks.
- No OSS, S3, database, or external object-store artifact backend.
- No remote HTTP URL ingestion, large binary chunking, or resumable upload protocol. Local file URL parts must stay inside the allowed workspace roots.
- No default hard failure for unsigned Agent Cards.
- No asymmetric Agent Card signing from the server and no automatic signing key rotation.
- No autonomous planner DAG or complex multi-agent orchestration.
- Push delivery is at-least-once for Redis-backed queues; callback receivers must handle duplicates and enforce their own endpoint-side authorization policy.

Tool permission requests are rejected automatically in A2A server mode. Run unauthenticated A2A mode only in trusted local environments or protect it with Bearer token, Basic auth, or API key authentication.
