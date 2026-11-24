---
title: Alibaba Cloud Credentials
description: Configure Alibaba Cloud credentials, including ECS RAM Role authentication.
---

# Alibaba Cloud Credentials

The steps below apply to both Desktop and Web; they use the same conversation interface.

## Configure cloud access in Desktop or Web

1. Open **Settings → Cloud credentials** and select **Alibaba Cloud**.

2. Choose the region for resource queries and deployment, for example `cn-hangzhou`.

3. Select the authentication method and fill the corresponding fields below.

4. For AccessKey, STS, RAM role, or ECS RAM Role, click **Save cloud credentials**. For OAuth, complete the [browser login](#oauth-browser-login) flow.

5. Return to the conversation and describe the cloud task. Saving credentials configures access; it does not create resources or grant additional RAM permissions.

| Method | Fields to provide |
|---|---|
| AccessKey | `AccessKeyId` and `AccessKeySecret`. |
| STS Token | `AccessKeyId`, `AccessKeySecret`, and `StsToken` from the same temporary credential. |
| RAM role | Source `AccessKeyId` and `AccessKeySecret`, target `RamRoleArn`, and optional `RamSessionName`. |
| ECS RAM Role | Optional role name; leave blank to discover it on the ECS instance running IaC Code. |
| OAuth browser login | Choose the account’s login site and complete browser authorization; do not enter tokens manually. |

Alibaba Cloud credentials are required for operations that inspect or manage cloud resources.

The `resolve_cloud_resource_selector` and `select_cloud_resource` tools use the same effective credential decision as `aliyun_api`. In A2A, request/session credentials take priority; when they are absent, the normal local, environment, and Alibaba Cloud CLI credential fallback applies. If no valid effective Alibaba Cloud credential is available, `aliyun_api` and both resource-selector tools are not registered.

## ECS RAM Role

Use **ECS RAM Role** when IaC Code runs on an Alibaba Cloud ECS instance that has a RAM role attached. IaC Code obtains temporary STS credentials from the ECS instance metadata service (IMDS), refreshes them automatically, and does not store an AccessKey ID, AccessKey secret, or STS token in its configuration.

In the Web or Desktop app, open **Settings > Cloud credentials**, choose **Alibaba Cloud**, then select **ECS RAM Role** as the authentication method.

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

1. Open **Settings → Cloud credentials**, select **Alibaba Cloud**, and choose **OAuth browser login**.

2. Select the region and the **Login site** that matches the account: China or International.

3. Click **Log in with browser** and finish login and authorization on the Alibaba Cloud page.

4. Return to IaC Code and wait for **Login succeeded**. The credentials and login site are saved by the login flow.

OAuth credentials refresh automatically when the access token or STS credentials approach expiration. If the refresh token expires or is revoked, authorize again from **Settings → Cloud credentials**.

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
