---
title: Alibaba Cloud Credentials
description: Configure Alibaba Cloud credentials, including ECS RAM Role authentication.
---

# Alibaba Cloud Credentials

Alibaba Cloud credentials are required for operations that inspect or manage cloud resources.

## ECS RAM Role

Use **ECS RAM Role** when IaC Code runs on an Alibaba Cloud ECS instance that has a RAM role attached. IaC Code obtains temporary STS credentials from the ECS instance metadata service (IMDS), refreshes them automatically, and does not store an AccessKey ID, AccessKey secret, or STS token in its configuration.

You can configure the mode from every user interface:

- In the REPL, run `/auth`, choose **Configure IaC Cloud Service**, then **Alibaba Cloud** and **ECS RAM Role**.
- In the Web or Desktop app, open **Settings > Cloud credentials**, choose **Alibaba Cloud**, then select **ECS RAM Role** as the authentication method.

Select the region used for cloud API calls. The ECS RAM role name is optional: leave it blank to discover the role attached to the instance through IMDS. A role name saved in IaC Code takes precedence over `ALIBABA_CLOUD_ECS_METADATA`; if neither is set, IaC Code asks IMDS to discover the role name.

The equivalent `.cloud-credentials.yml` configuration is:

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # Optional; omit or leave empty for auto-discovery
```

IaC Code also recognizes the active profile in `~/.aliyun/config.json` when its `mode` is `EcsRamRole`; `ram_role_name` remains optional there as well.

The configuration can be saved on any machine, but cloud API calls succeed only where ECS IMDS is reachable and the instance has a matching RAM role. The role's attached RAM policies determine which APIs are allowed.

## OAuth Browser Login

The recommended interactive setup path is `/auth`:

```text
/auth
```

Choose **Configure IaC Cloud Service**, then **Alibaba Cloud**, then **OAuth Login (Browser)**. IaC Code opens a browser authorization flow, listens for the local callback, exchanges the authorization code with PKCE, and saves OAuth-backed temporary credentials to `.cloud-credentials.yml` under the IaC Code config directory.

During setup you can choose the China or International OAuth site. IaC Code stores the selected site with the refresh token so future refreshes use the same endpoint.

OAuth credentials are refreshed automatically when the access token or STS credentials are near expiration. If the refresh token expires or is revoked, run `/auth` again and choose OAuth Login (Browser).

## Environment Variables

Environment variables are supported for AccessKey, STS, and ECS RAM Role workflows:

| Variable | Description |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token; switches the credential mode to STS when set |
| `ALIBABA_CLOUD_REGION_ID` | Default region |
| `ALIBABA_CLOUD_ECS_METADATA` | Optional ECS RAM role name used when the configured mode is `EcsRamRole` and no role name is saved; does not select the mode by itself |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | Set to `true` to disable ECS instance metadata credentials |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | Set to `true` to require IMDSv2 and disable fallback to IMDSv1 |

Use test or temporary credentials when experimenting. Do not paste production secrets into shell history, screenshots, logs, or issue reports.
