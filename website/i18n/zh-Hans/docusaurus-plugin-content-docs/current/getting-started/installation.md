---
title: 安装
description: 安装 IaC Code 并验证命令可用。
---

# 安装

IaC Code 需要 Python 3.12 或更高版本。

从已配置的 Python 包索引安装：

```bash
pip install iac-code \
  --extra-index-url http://yum.tbsite.net/aliyun-pypi/simple/ \
  --trusted-host yum.tbsite.net
```

验证命令可用：

```bash
iac-code --help
```
