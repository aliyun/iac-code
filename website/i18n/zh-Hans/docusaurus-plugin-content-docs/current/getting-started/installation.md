---
title: 安装
description: 安装 IaC Code 并验证命令可用。
---

# 安装

## 安装桌面版

需要图形应用时，下载适合操作系统的[桌面安装包](../desktop-app.md)。安装包已包含 Python 运行时，无需自行安装 Python 或执行 pip。安装步骤和各平台要求见桌面版文档。

## 安装 Web 与 headless

使用本地 Web 或命令行自动化时，先安装 Python 3.10 或更高版本，再安装下方的软件包。该方式支持 macOS、Linux 和 Windows。

```bash
pip install iac-code
```

验证命令可用：

```bash
iac-code --help
```

在交互终端中运行 `iac-code` 即可启动 Web 并打开浏览器；使用 `iac-code web --no-open` 可只启动服务。下一步见[快速开始](./quick-start.md)。

## 可选功能

默认安装已包含 Web 和 HTTP 运行时依赖。`[http]` 扩展保留为兼容别名，无需单独安装。在仓库检出环境中运行 `uv sync` 即可。

其他传输仍使用可选扩展，例如通过 `pip install 'iac-code[a2a-grpc]'` 安装 gRPC 支持，或通过 `pip install 'iac-code[a2a-redis]'` 安装 Redis 支持。

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
