---
title: 安装
description: 安装 IaC Code 并验证命令可用。
---

# 安装

IaC Code 需要 Python 3.10 或更高版本。支持 macOS、Linux 和 Windows。

## 安装

从已配置的 Python 包索引安装：

```bash
pip install iac-code
```

验证命令可用：

```bash
iac-code --help
```

## 可选功能

交互式 CLI 使用基础包即可运行。部分运行模式依赖可选扩展，通过 `iac-code[<extra>]` 语法安装：

| 扩展 | 启用 | 命令 |
|---|---|---|
| `http` | 本地 [Web 应用](../web-app.md)（`iac-code web`） | `pip install 'iac-code[http]'` |
| `a2a` | [A2A 服务器](../a2a/getting-started.md)（`iac-code a2a`） | `pip install 'iac-code[a2a]'` |

如果在未安装对应扩展的情况下启动某个运行模式，命令会失败并提示需要安装的扩展。在仓库检出环境中开发时，请改用 `uv sync --extra <extra>`。

## Windows 要求

在 Windows 上需要安装 [Git for Windows](https://gitforwindows.org/) 以提供工具执行所需的 bash 环境。

如果 Git Bash 已安装但不在 PATH 中，请将 `IAC_CODE_GIT_BASH_PATH` 环境变量设置为 `bash.exe` 的路径：

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

可以通过 winget 安装 Git for Windows：

```powershell
winget install --id Git.Git -e --source winget
```
