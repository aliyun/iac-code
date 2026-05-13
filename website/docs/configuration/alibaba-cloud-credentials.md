---
title: Alibaba Cloud Credentials
description: Configure Alibaba Cloud AccessKey or STS credentials.
---

# Alibaba Cloud Credentials

Alibaba Cloud credentials are required for operations that inspect or manage cloud resources.

Supported environment variables:

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token; switches the credential mode to STS when set |
| `ALIBABA_CLOUD_REGION_ID` | Default region |

Use test or temporary credentials when experimenting. Do not paste production secrets into shell history, screenshots, logs, or issue reports.
