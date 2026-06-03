---
title: 阿里云凭证
description: 配置阿里云 AccessKey 或 STS 凭证。
---

# 阿里云凭证

需要检查或管理云资源时，必须配置阿里云凭证。

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

实验时请使用测试凭证或临时凭证。不要把生产密钥粘贴到 shell 历史、截图、日志或 issue 报告中。
