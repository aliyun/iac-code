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

You can relocate it by setting the `IAC_CODE_CONFIG_DIR` environment variable (supports `~` and `$VAR` expansion). When set, every persisted artifact — credentials, settings, history, `projects/`, `image-cache/`, `tool-results/`, `memory/`, `a2a/`, `telemetry/`, `skills/` — follows the new location. Startup/debug logs default to `<config-dir>/logs/` but can be moved separately with `IAC_CODE_LOG_DIR`; permission audit records stay under `<config-dir>/logs/`.

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
  audit:
    include_tool_input: false
    max_file_bytes: 10485760
    max_files: 5
```

| Field | Description |
|---|---|
| `mode` | Permission mode: `default`, `accept_edits`, `bypass_permissions`, `dont_ask`. |
| `allow` | List of tool permission patterns to auto-approve. |
| `deny` | List of tool permission patterns to auto-deny. |
| `ask` | List of tool permission patterns that always require confirmation. |
| `additional_directories` | Extra directories beyond cwd that the agent is allowed to write to. |
| `audit` | Local permission audit log settings. |

### Pattern Syntax

Tool permission patterns follow the format `tool_name(rule)`:

| Pattern | Meaning |
|---|---|
| `bash` | Match all bash commands (bare tool name). |
| `bash(git *)` | Match bash commands starting with `git`. |
| `bash(curl:*)` | Match bash commands starting with `curl`. |
| `write_file` | Match all write_file tool calls. |
| `aliyun_api(ros:CreateStack)` | Match one Alibaba Cloud API product/action pair. |

Rules are evaluated in order: **deny → ask → allow → default behavior**. CLI arguments (`--allowed-tools`, `--disallowed-tools`) take the highest precedence.

### Alibaba Cloud API Permissions

`aliyun_api` distinguishes read-only API calls from calls that may modify cloud resources. Read-only API actions are allowed automatically. Non-read-only API calls require confirmation or an exact allow rule for that product/action, for example:

```yaml
permissions:
  allow:
    - "aliyun_api(ros:CreateStack)"
```

A bare `aliyun_api` allow rule does not blanket-approve Alibaba Cloud write APIs. Outside `bypass_permissions`, write allow rules must match the exact canonical `product:action` pair. In `bypass_permissions` mode, protected Alibaba Cloud write APIs are auto-approved, but every allow decision that requires an audit record still fails closed if audit persistence fails. Wildcards can still be useful for deny or ask rules, and for read-only rule matching.

ROA-style requests are treated as read-only only when the method is `GET` and the request has no body. Non-read-only ROA requests follow the same exact canonical `product:action` allow-rule requirement as RPC-style write APIs: an exact rule such as `aliyun_api(cs:CreateCluster)` can approve the write, while wildcard allow rules still do not approve non-read-only calls.

### Permission Audit Log

Permission decisions that cross user prompts, tool-cache boundaries, automation approval, or resolver approval are appended to:

```text
<config-dir>/logs/permission-audit.jsonl
```

By default, this is `~/.iac-code/logs/permission-audit.jsonl`. The permission audit log follows `IAC_CODE_CONFIG_DIR`; `IAC_CODE_LOG_DIR` only moves startup/debug logs. The audit writer appends JSONL records with file locking, rotates the file, and restricts local file permissions where the operating system supports it. Routine read-only auto-allow decisions may be omitted, but denials, prompts, cached decisions, automation approvals, resolver approvals, and other audited permission boundaries are recorded.

Audit settings are configured under `permissions.audit`:

| Field | Default | Description |
|---|---:|---|
| `include_tool_input` | `false` | Include shape-only tool input in JSONL audit records. String values are stored as type, length, and fingerprint; secret-looking keys are redacted; non-whitelisted field names may be fingerprinted; raw business payload strings are not written. Alibaba Cloud API entries also keep a safe operation summary. |
| `max_file_bytes` | `10485760` | Rotate `permission-audit.jsonl` when it grows past this size. |
| `max_files` | `5` | Number of rotated audit files to keep. Values above the built-in maximum are clamped. |

If any allow decision that requires an audit record cannot be persisted to the audit log, IaC Code fails closed and denies the action instead of executing it without an audit trail.
