---
sidebar_position: 3
title: Tools, Resources, Prompts, and Skills
description: Understand how MCP capabilities appear inside IaC Code.
---

# Tools, Resources, Prompts, and Skills

Connected MCP servers can expose four kinds of capabilities to IaC Code.

## Tools

Each MCP tool becomes an IaC Code tool:

```text
mcp__<server>__<tool>
```

Tool descriptions and JSON input schemas come from the MCP server. IaC Code forwards the model's tool input to the MCP server, then converts MCP content blocks into a normal tool result.

MCP tool annotations are honored where possible:

| MCP annotation | IaC Code behavior |
|---|---|
| `readOnlyHint: true` | The tool is treated as read-only and concurrency-safe. |
| `destructiveHint: true` | The tool is treated as destructive for permission decisions. |

MCP tools still pass through IaC Code's existing permission system. Configure permission policy with normal `permissions` settings or CLI flags such as `--allowed-tools`, `--disallowed-tools`, and `--permission-mode`.

MCP progress notifications are surfaced in interactive rendering, headless progress output, ACP tool progress updates, and A2A tool metadata.

## Tool Results and Artifacts

IaC Code converts MCP content blocks into model-visible text:

| MCP content | IaC Code result |
|---|---|
| Text content | Included directly in the tool result. |
| `structuredContent` | Rendered as formatted JSON under a structured-content section. |
| Text resources | Rendered with server and URI provenance. |
| `resource_link` | Rendered as a resource link with URI and MIME type. |
| Image, audio, and blob data | Stored as private artifact files and referenced by artifact id. |

Binary artifacts are stored below:

```text
<config-dir>/tool-results/<session-id>/mcp/<server>/<tool>/
```

The model sees the artifact id and metadata, not raw base64 data.

## Resources

When any connected server exposes resources, IaC Code registers two global tools:

| Tool | Purpose |
|---|---|
| `list_mcp_resources` | Lists resources from connected MCP servers. Optionally filter by server name. |
| `read_mcp_resource` | Reads one resource by `server` and `uri`. |

Resource lines include server name, URI, optional resource name, and optional MIME type.

## Prompts

MCP prompts become slash commands:

```text
/mcp__<server>__<prompt> key=value
```

When invoked, IaC Code calls MCP `prompts/get`, renders the returned prompt messages, injects the rendered prompt into the conversation, and lets the model continue. Prompt arguments can be passed as:

```text
template_name=prod-vpc region=cn-hangzhou
```

or as JSON:

```json
{"template_name": "prod-vpc", "region": "cn-hangzhou"}
```

Required prompt arguments are validated before the MCP call. Quoted values are supported, including Windows paths with backslashes.

## Skills

MCP resources with `skill://` URIs become skill commands:

```text
$mcp__<server>__<skill>
```

IaC Code reads the remote skill resource, parses frontmatter, and registers it as a normal skill command. Remote MCP skills are safety-limited:

- Remote `allowed_tools` are cleared.
- Remote auto-trigger path rules are cleared.
- Remote skill body and description length are bounded.
- If the remote skill conflicts with an existing command, it is skipped with an MCP warning.

MCP skill resources may be read during startup so the command can be registered before the user invokes it.

## Dynamic Updates

If an MCP server sends `tools/list_changed`, `resources/list_changed`, or `prompts/list_changed`, IaC Code refreshes the affected capability list and updates the tool or command registry. Refresh failures are reported as MCP warnings and do not stop the active session.
