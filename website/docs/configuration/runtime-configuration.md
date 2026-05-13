---
title: Configuration
description: Runtime configuration order and local files.
---

# Configuration

IaC Code reads configuration from CLI arguments, environment variables, and files in the runtime configuration directory.

Configuration precedence:

```text
CLI arguments > environment variables > configuration files
```

The runtime directory is:

```text
~/.iac-code/
```

Common files:

| File | Description |
|---|---|
| `.credentials.yml` | LLM credentials |
| `.cloud-credentials.yml` | Cloud provider credentials |
| `settings.yml` | Selected provider, model, and related settings |
| history files | Input history for interactive workflows |

Avoid committing or sharing files from this directory because they can contain secrets or local preferences.
