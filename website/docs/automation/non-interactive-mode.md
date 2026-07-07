---
title: Non-interactive Mode
description: Run one-shot prompts from arguments or stdin.
---

# Non-interactive Mode

Non-interactive mode runs a single prompt and exits. Use it when you want IaC Code to produce output for a repeatable task without staying in the REPL.

Use `--prompt` to pass the prompt directly:

```bash
iac-code --prompt "Create an OSS Bucket"
```

Use `--prompt -` to read the prompt from standard input:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```

Use `--output-format` when the caller needs structured output:

```bash
iac-code --prompt "Create an OSS Bucket" --output-format json
```

Use `--max-turns` to bound how long the agent can work:

```bash
iac-code --prompt "Create a VPC" --max-turns 20
```

Supported output formats are:

| Format | Purpose |
|---|---|
| `text` | Human-readable output. This is the default. |
| `json` | A single JSON result for callers that parse the final response. |
| `stream-json` | Streaming JSON events for callers that process incremental progress. |

## SDK Process Mode

SDK process mode is for clients that keep `iac-code` running as a subprocess and exchange line-delimited JSON over stdin/stdout:

```bash
iac-code --input-format stream-json --output-format stream-json
```

This is separate from one-shot `--prompt` mode. `--input-format stream-json` requires `--output-format stream-json` and rejects `--prompt`. The caller must send an `initialize` control request before user messages:

```json
{"type":"control_request","request_id":"req-init","request":{"subtype":"initialize","cwd":"/absolute/workspace","model":"qwen3.7-max"}}
{"type":"user","request_id":"req-1","session_id":"session-1","message":{"role":"user","content":"Create an OSS Bucket"},"metadata":{"iac_code":{"cwd":"/absolute/workspace"}}}
{"type":"control_request","request_id":"req-end","request":{"subtype":"end_session"}}
```

The process writes `control_response`, `stream_event`, `error`, and final `result` frames. Stream events reuse the public `stream-json` event shape used by one-shot output streaming. `cwd` values in initialize payloads or `metadata.iac_code.cwd` must be absolute paths.

Supported control flows include `initialize`, `interrupt`, `set_model`, `end_session`, `close`, `keep_alive`, and `update_environment_variables`. Only one user turn can run at a time inside a process. If two subprocesses try to use the same `session_id` concurrently for the same `cwd`, the second turn receives a retryable `session_busy` error.

When `IAC_CODE_MODE=pipeline`, process mode executes Pipeline mode instead of the normal agent loop. Pipeline stream frames use `type: "pipeline_event"`, and final result frames include a `pipeline` object with `contextId`, `taskId`, `iacCodeSessionId`, `status`, and `sidecarStatus`. For a recoverable pipeline follow-up, send the same `contextId` and the active `taskId`; if the context has a recoverable task and the task id is omitted, the process returns a retryable `pipeline_task_required` error with `recoverableTaskId`.

## Session Backups

When `IAC_CODE_CONFIG_BACKUP_DIR` is set, non-interactive runs mirror the v2 session at key checkpoints. The ordinary end-of-turn checkpoint uses `normal_turn_end`; backup failures at that point are logged as a `warning` and recorded in `.backup-state.json` without failing the completed response or adding a warning field to the final output. Pipeline mode has its own critical backup gates.

## Permission Control in Automation

When running non-interactively, use `--permission-mode` to control how the agent handles tool approvals:

```bash
iac-code --prompt "Deploy the stack" --permission-mode bypass_permissions
```

In `bypass_permissions`, tool actions are auto-approved except safety checks, but every allow decision that requires an audit record still fails closed if audit persistence fails. Alibaba Cloud write APIs are still protected outside `bypass_permissions`: for narrower trusted automation, stay outside `bypass_permissions` and allow each required write API explicitly:

```bash
iac-code --prompt "Deploy the stack" \
  --allowed-tools 'aliyun_api(ros:CreateStack)' \
  --permission-mode dont_ask
```

To restrict what the agent can do, combine `--allowed-tools` and `--disallowed-tools`:

```bash
iac-code --prompt "Check the stack status" \
  --allowed-tools 'bash(git *),bash(ls:*)' \
  --disallowed-tools 'bash(rm *)' \
  --permission-mode dont_ask
```

For all startup flags, see [Command Line Options](../cli/command-line-options.md).
