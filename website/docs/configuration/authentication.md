---
title: Authentication
description: Configure LLM and cloud credentials with the auth flow.
---

# Authentication

Run `iac-code` in a terminal to start the local Web app and open your browser. On first use, configure the LLM provider and Alibaba Cloud credentials in Settings.

- **Settings → Models**: choose the provider, enter its API key, and select a model.
- **Settings → Cloud credentials**: choose Alibaba Cloud, its authentication method, and the region.

Web, Desktop, and headless runs share these saved credentials.

For terminal setup, start `iac-code tui` and run `/auth`.

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
