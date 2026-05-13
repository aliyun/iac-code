---
title: 阿里云凭证
description: 配置阿里云 AccessKey 或 STS 凭证。
---

# 阿里云凭证

需要检查或管理云资源时，必须配置阿里云凭证。

支持的环境变量：

| 变量 | 说明 |
|---|---|
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | AccessKey Secret |
| `ALIBABA_CLOUD_SECURITY_TOKEN` | STS token；设置后凭证模式切换为 STS |
| `ALIBABA_CLOUD_REGION_ID` | 默认地域 |

实验时请使用测试凭证或临时凭证。不要把生产密钥粘贴到 shell 历史、截图、日志或 issue 报告中。
