---
title: 快速开始
description: 配置 IaC Code 并运行第一个提示词。
---

# 快速开始

运行交互式 CLI：

```bash
iac-code
```

首次使用时，配置 LLM 提供商和阿里云凭证：

```text
/auth
```

然后提出基础设施需求：

```text
创建一个 VPC 和两台 ECS 实例
```

如需一次性提示词，使用非交互模式：

```bash
iac-code --prompt "创建一个 OSS Bucket"
```

也可以从标准输入读取提示词：

```bash
echo "创建一个 VPC 和两台 ECS 实例" | iac-code --prompt -
```
