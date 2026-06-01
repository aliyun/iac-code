---
title: 贡献指南
description: 如何搭建本地环境并参与 IaC Code 贡献。
---

# 贡献指南

## 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## 搭建环境

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` 会安装所有依赖并配置 pre-commit 钩子（每次提交自动执行 lint 和 format 检查）。

## 开发流程

以调试模式运行：

```bash
make dev
```

运行测试：

```bash
make test           # 默认 Python 版本
make test PY=3.12   # 指定版本
make test PY=all    # 所有支持的版本（3.10–3.14）
```

代码质量：

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

覆盖率：

```bash
make coverage
```

## 项目结构

```
src/iac_code/       # 源代码
tests/              # 测试
website/            # 文档站点（Docusaurus）
```

## 提交变更

1. Fork 仓库并创建功能分支。
2. 编写代码并补充测试。
3. 运行 `make format`，然后确保 `make lint` 和 `make test` 通过。
4. 向 `main` 分支提交 Pull Request。
