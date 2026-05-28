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
