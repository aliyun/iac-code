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

[Interactive Mode](./interactive-mode.md):

```bash
iac-code tui
iac-code tui --help
```

`iac-code` opens Web by default. Put terminal options after `iac-code tui` (for example, `iac-code tui --resume <id>`). Headless `--prompt` and stream-json flags stay on the root command. Without a terminal, start Web explicitly with `iac-code web --no-open`.

```bash
iac-code --prompt "Create an OSS Bucket"
iac-code --input-format stream-json --output-format stream-json
```
