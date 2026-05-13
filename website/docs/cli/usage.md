---
title: CLI Overview
description: Start IaC Code from the terminal and choose the right workflow.
---

# CLI Overview

Run `iac-code` from the terminal:

```bash
iac-code
```

The CLI supports two workflows:

| Workflow | Use it when |
|---|---|
| [Interactive Mode](./interactive-mode.md) | You want to refine infrastructure requirements over multiple turns in a REPL. |
| [Non-interactive Mode](../automation/non-interactive-mode.md) | You want to run a single prompt and return output to a caller. |

Common startup commands:

```bash
iac-code
iac-code --prompt "Create an OSS Bucket"
echo "Create a VPC" | iac-code --prompt -
iac-code --debug
```

Use [Command Line Options](./command-line-options.md) for startup flags and [Slash Commands](./commands.md) for commands available inside an interactive session.
