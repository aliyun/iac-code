---
title: Quick Start
description: Configure IaC Code and run your first prompt.
---

# Quick Start

Run `iac-code` in a terminal to start the local Web app and open your browser. On first use, configure the LLM provider and Alibaba Cloud credentials in Settings.

```bash
iac-code
```

Then ask for infrastructure:

```text
Create a VPC and two ECS instances
```

For a one-shot prompt, use non-interactive mode:

```bash
iac-code --prompt "Create an OSS Bucket"
```

You can also read the prompt from standard input:

```bash
echo "Create a VPC and two ECS instances" | iac-code --prompt -
```
