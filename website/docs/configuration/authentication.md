---
title: Authentication
description: Configure LLM and cloud credentials with the auth flow.
---

# Authentication

Use `/auth` in interactive mode to configure both model provider access and Alibaba Cloud access.

```bash
iac-code
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
