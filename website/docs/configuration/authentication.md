---
title: Authentication
description: Configure LLM and cloud credentials with the auth flow.
---

# Authentication

Run `iac-code` in a terminal to start the local Web app and open your browser. On first use, configure the LLM provider and Alibaba Cloud credentials in Settings.

The terminal UI is retained as `iac-code tui`, but is no longer updated or maintained. Use the Web app or Desktop app for ongoing use.

Use `/auth` in interactive mode to configure both model provider access and Alibaba Cloud access.

```bash
iac-code tui
```

```text
/auth
```

The auth flow guides you through provider selection, API key input, model selection, and Alibaba Cloud credential setup.

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
