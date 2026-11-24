---
title: CLI Overview
description: Start IaC Code from the terminal and choose the right workflow.
---

# CLI Overview

Run `iac-code` in a terminal to start the local Web app and open your browser. On first use, configure the LLM provider and Alibaba Cloud credentials in Settings.

```bash
iac-code
iac-code web --no-open
```

Choose normal chat or a pipeline in Web or Desktop when creating a conversation. For automation, use a one-shot prompt or a stream-json subprocess:

```bash
iac-code --prompt "Generate a VPC template"
iac-code --input-format stream-json --output-format stream-json
```

See [command-line options](./command-line-options.md) and [headless automation](../automation/non-interactive-mode.md). Without an interactive terminal, start Web explicitly with `iac-code web --no-open`.

For compatibility with older terminal workflows, see [Legacy Terminal Interface](./interactive-mode.md).
