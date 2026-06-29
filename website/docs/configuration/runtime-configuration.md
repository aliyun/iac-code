---
title: Configuration
description: Runtime configuration order and local files.
---

# Configuration

IaC Code reads configuration from CLI arguments, environment variables, and files in the runtime configuration directory.

Configuration precedence:

```text
CLI arguments > environment variables > configuration files
```

The runtime directory defaults to:

```text
~/.iac-code/
```

You can relocate it by setting the `IAC_CODE_CONFIG_DIR` environment variable (supports `~` and `$VAR` expansion). When set, every persisted artifact — credentials, settings, history, `projects/`, `image-cache/`, `tool-results/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — follows the new location. Logs default to `<config-dir>/logs/` but can be moved separately with `IAC_CODE_LOG_DIR`.

Common files:

| File | Description |
|---|---|
| `.credentials.yml` | LLM credentials |
| `.cloud-credentials.yml` | Cloud provider credentials |
| `settings.yml` | Selected provider, model, and related settings |
| `AGENTS.md` | User memory loaded as persistent instructions |
| history files | Input history for interactive workflows |

Avoid committing or sharing files from this directory because they can contain secrets or local preferences.

## Memory Files

IaC Code has two public memory locations:

| Location | Purpose |
|---|---|
| `<project-root>/AGENTS.md` | Project memory. This can be committed when the instructions are useful for everyone working in the project. |
| `<config-dir>/AGENTS.md` | User memory. This follows `IAC_CODE_CONFIG_DIR` and is private to the local user. |

Set `IAC_CODE_INSTRUCTION_MEMORY_FILE` to use another instruction memory filename, for example `IAC-CODE.md`.

Project auto-memory topic files are stored under:

```text
<config-dir>/projects/<project-key>/memory/
```

`MEMORY.md` in that folder is the topic index used by auto-memory side calls. It is not loaded as always-on context. When auto-memory is on, IaC Code may select relevant topic files and add them as hidden conversation context.

## Project Settings

In addition to the user-level `~/.iac-code/settings.yml`, IaC Code loads project-level settings from the current working directory:

| File | Scope |
|---|---|
| `.iac-code/settings.yml` | Shared project settings (safe to commit). |
| `.iac-code/settings.local.yml` | Local overrides (should be git-ignored). |

Merge order: **user settings → project settings → project local settings → CLI arguments** (later sources override earlier ones).

## Provider Request Policy

Provider entries in `settings.yml` can include request policy fields for OpenAI-compatible providers. These settings are useful when a model separates visible answer tokens from reasoning/thinking tokens.

```yaml
activeProvider: dashscope
providers:
  dashscope:
    model: glm-5.2
    thinkingBudget: 8192
    maxCompletionTokens: 16384
    models:
      kimi-k2.7-code:
        thinkingBudget: 8192
        maxCompletionTokens: 16384
```

| Field | Scope | Description |
|---|---|---|
| `thinkingBudget` | Provider or model | Positive integer reasoning/thinking budget passed to providers that support it. |
| `maxCompletionTokens` | Provider or model | Positive integer `max_completion_tokens` override for providers/models that use that request field. |
| `effort` | Provider or model | Optional thinking effort override for models that support effort control. |

Model-level values under `providers.<provider>.models.<model>` override provider-level values when they are valid. Invalid numeric values are ignored, so IaC Code falls back to the provider-level value or built-in model policy.

For Alibaba Cloud DashScope and DashScope Token Plan, IaC Code has a built-in `thinkingBudget` of `8192` for `glm-5.2` and `kimi-k2.7-code`. When `maxCompletionTokens` is not set, the request limit is computed as the normal answer token limit plus the effective thinking budget.

## Tool Permission Configuration

The `permissions` section in `settings.yml` configures which tool actions are allowed, denied, or require confirmation:

```yaml
permissions:
  mode: default
  allow:
    - "bash(git *)"
    - "bash(ls:*)"
  deny:
    - "bash(rm -rf *)"
  ask:
    - "bash(curl:*)"
  additional_directories:
    - "/tmp/workspace"
```

| Field | Description |
|---|---|
| `mode` | Permission mode: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | List of tool permission patterns to auto-approve. |
| `deny` | List of tool permission patterns to auto-deny. |
| `ask` | List of tool permission patterns that always require confirmation. |
| `additional_directories` | Extra directories beyond cwd that the agent is allowed to write to. |

### Pattern Syntax

Tool permission patterns follow the format `tool_name(rule)`:

| Pattern | Meaning |
|---|---|
| `bash` | Match all bash commands (bare tool name). |
| `bash(git *)` | Match bash commands starting with `git`. |
| `bash(curl:*)` | Match bash commands starting with `curl`. |
| `write_file` | Match all write_file tool calls. |

Rules are evaluated in order: **deny → ask → allow → default behavior**. CLI arguments (`--allowed-tools`, `--disallowed-tools`) take the highest precedence.
