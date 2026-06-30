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
