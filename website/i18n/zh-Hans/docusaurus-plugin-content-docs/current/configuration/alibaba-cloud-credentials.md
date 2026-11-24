---
title: 阿里云凭证
description: 配置阿里云凭证，包括 ECS RAM Role 授权。
---

# 阿里云凭证

以下步骤同时适用于桌面版和 Web，两者使用同一套对话界面。

## 在桌面版或 Web 中配置云访问

1. 打开**设置 → 云凭证**，选择**阿里云**。

2. 选择资源查询和部署使用的地域，例如 `cn-hangzhou`。

3. 选择认证方式，按下表填写对应字段。

4. 使用 AccessKey、STS、RAM 角色或 ECS RAM 角色时，单击**保存云凭证**；使用 OAuth 时，按下方“OAuth 浏览器登录”的步骤完成授权。

5. 返回对话并描述云资源任务。保存凭证只完成访问配置，不会创建资源，也不会增加账号的 RAM 权限。

| 认证方式 | 需要填写的内容 |
|---|---|
| AccessKey | `AccessKeyId` 和 `AccessKeySecret`。 |
| STS Token | 同一组临时凭证的 `AccessKeyId`、`AccessKeySecret` 和 `StsToken`。 |
| RAM 角色 | 来源 `AccessKeyId`、`AccessKeySecret`，目标 `RamRoleArn`，以及可选的 `RamSessionName`。 |
| ECS RAM 角色 | 可选的角色名称；留空时从运行 IaC Code 的 ECS 实例元数据中发现。 |
| OAuth 浏览器登录 | 选择账号所属登录站点，在浏览器完成授权，无需手工填写令牌。 |

需要检查或管理云资源时，必须配置阿里云凭证。

`resolve_cloud_resource_selector` 和 `select_cloud_resource` 与 `aliyun_api` 使用相同的有效凭证判定。A2A 中请求/会话凭证优先；未提供时，按正常顺序回退到本地配置、环境变量和阿里云 CLI 凭证。如果没有有效的阿里云凭证，`aliyun_api` 和两个资源选择器工具都不会注册。

## ECS RAM Role 授权

当 IaC Code 运行在已绑定 RAM 角色的阿里云 ECS 实例上时，可以使用 **ECS RAM Role**。IaC Code 会从 ECS 实例元数据服务（IMDS）获取临时 STS 凭证并自动刷新，配置文件中不会保存 AccessKey ID、AccessKey Secret 或 STS token。

在 Web 或 Desktop 应用中打开 **设置 > 云凭证**，选择 **Alibaba Cloud**，再将认证方式设为 **ECS RAM Role**。

请选择云 API 调用使用的地域。ECS RAM 角色名称是可选的：留空时会通过 IMDS 自动发现实例绑定的角色。IaC Code 中保存的角色名称优先于 `ALIBABA_CLOUD_ECS_METADATA`；两者均未设置时，IaC Code 会请求 IMDS 自动发现角色名称。

等效的 `.cloud-credentials.yml` 配置如下：

```yaml
aliyun:
  mode: EcsRamRole
  region_id: cn-beijing
  ram_role_name: MyEcsRole # 可选；省略或留空时自动发现
```

如果 `~/.aliyun/config.json` 中当前 profile 的 `mode` 为 `EcsRamRole`，IaC Code 也会识别该配置；其中的 `ram_role_name` 同样可以省略。

配置可以在任意机器上保存，但只有运行环境能够访问 ECS IMDS，且实例绑定了匹配的 RAM 角色时，云 API 调用才能成功。角色上绑定的 RAM 权限策略决定允许调用哪些 API。

## OAuth 浏览器登录

1. 打开**设置 → 云凭证**，选择**阿里云**和 **OAuth 浏览器登录**。

2. 选择地域，在**登录站点**中选择账号所属的中国站或国际站。

3. 单击**用浏览器登录**，在打开的阿里云页面完成登录与授权。

4. 返回 IaC Code，等待登录成功提示。登录流程会保存凭证和所选站点。

当 access token 或 STS 凭证即将过期时，OAuth 凭证会自动刷新。如果 refresh token 过期或被撤销，请在**设置 → 云凭证**中重新授权。

## 环境变量

支持的环境变量：

| 变量 | 说明 |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token；设置后凭证模式切换为 STS |
| `ALIBABA_CLOUD_REGION_ID` | 默认地域 |
| `ALIBABA_CLOUD_ECS_METADATA` | 可选的 ECS RAM 角色名称；仅在模式已配置为 `EcsRamRole` 且未保存角色名称时使用，不会自行选择认证模式 |
| `ALIBABA_CLOUD_ECS_METADATA_DISABLED` | 设为 `true` 可禁用 ECS 实例元数据凭证 |
| `ALIBABA_CLOUD_IMDSV1_DISABLED` | 设为 `true` 可要求使用 IMDSv2，并禁止回退到 IMDSv1 |

实验时请使用测试凭证或临时凭证。不要把生产密钥粘贴到 shell 历史、截图、日志或 issue 报告中。
