---
title: Authentication
description: Configure model and cloud access in Desktop or Web settings.
---

# Authentication

Open Desktop, or run `iac-code` in an interactive terminal to open Web. Configure model access and any cloud credentials in Settings.

- [Configure a provider in Desktop or Web](./llm-providers.md)
- [Configure cloud access in Desktop or Web](./alibaba-cloud-credentials.md)

Web, Desktop, and headless runs share these saved credentials.

Runtime configuration is stored under the user configuration directory:

```text
~/.iac-code/
```

Important files include:

| File | Purpose |
|---|---|
| `.credentials.yml` | LLM provider credentials |
| `.cloud-credentials.yml` | Alibaba Cloud credentials |
| `settings.yml` | Runtime settings |
