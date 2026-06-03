---
title: Alibaba Cloud Credentials
description: Configure Alibaba Cloud AccessKey or STS credentials.
---

# Alibaba Cloud Credentials

Alibaba Cloud credentials are required for operations that inspect or manage cloud resources.

## OAuth Browser Login

The recommended interactive setup path is `/auth`:

```text
/auth
```

Choose **Configure IaC Cloud Service**, then **Alibaba Cloud**, then **OAuth Login (Browser)**. IaC Code opens a browser authorization flow, listens for the local callback, exchanges the authorization code with PKCE, and saves OAuth-backed temporary credentials to `.cloud-credentials.yml` under the IaC Code config directory.

During setup you can choose the China or International OAuth site. IaC Code stores the selected site with the refresh token so future refreshes use the same endpoint.

OAuth credentials are refreshed automatically when the access token or STS credentials are near expiration. If the refresh token expires or is revoked, run `/auth` again and choose OAuth Login (Browser).

## Environment Variables

Environment variables are still supported for AccessKey and STS workflows:

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token; switches the credential mode to STS when set |
| `ALIBABA_CLOUD_REGION_ID` | Default region |

Use test or temporary credentials when experimenting. Do not paste production secrets into shell history, screenshots, logs, or issue reports.
