---
title: 阿里云凭证
description: 配置阿里云凭证，包括 ECS RAM Role 授权。
---

# 阿里云凭证

需要检查或管理云资源时，必须配置阿里云凭证。

## ECS RAM Role 授权

当 IaC Code 运行在已绑定 RAM 角色的阿里云 ECS 实例上时，可以使用 **ECS RAM Role**。IaC Code 会从 ECS 实例元数据服务（IMDS）获取临时 STS 凭证并自动刷新，配置文件中不会保存 AccessKey ID、AccessKey Secret 或 STS token。

所有用户界面都可以配置该模式：

- 在 REPL 中运行 `/auth`，依次选择 **配置 IaC 云服务**、**Alibaba Cloud** 和 **ECS RAM Role**。
- 在 Web 或 Desktop 应用中打开 **设置 > 云凭证**，选择 **Alibaba Cloud**，再将认证方式设为 **ECS RAM Role**。

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

推荐的交互式配置入口是 `/auth`：

```text
/auth
```

选择 **配置 IaC 云服务**，然后选择 **Alibaba Cloud**，再选择 **OAuth Login (Browser)**。IaC Code 会打开浏览器授权流程，等待本地回调，使用 PKCE 交换授权码，并将基于 OAuth 的临时凭证保存到 IaC Code 配置目录下的 `.cloud-credentials.yml`。

配置过程中可以选择中国站或国际站 OAuth。IaC Code 会把所选站点与 refresh token 一起保存，后续刷新会继续使用同一 endpoint。

当 access token 或 STS 凭证即将过期时，OAuth 凭证会自动刷新。如果 refresh token 过期或被撤销，请重新运行 `/auth` 并选择 OAuth Login (Browser)。

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
