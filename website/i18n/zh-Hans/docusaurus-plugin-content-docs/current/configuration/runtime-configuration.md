---
title: 配置
description: 运行时配置顺序和本地文件。
---

# 配置

IaC Code 会从 CLI 参数、环境变量以及运行时配置目录中的文件读取配置。

配置优先级：

```text
CLI 参数 > 环境变量 > 配置文件
```

运行时目录为：

```text
~/.iac-code/
```

常见文件：

| 文件 | 说明 |
|---|---|
| `.credentials.yml` | LLM 凭证 |
| `.cloud-credentials.yml` | 云厂商凭证 |
| `settings.yml` | 已选择的提供商、模型和相关设置 |
| history files | 交互式工作流的输入历史 |

避免提交或分享该目录中的文件，因为它们可能包含密钥或本地偏好。
