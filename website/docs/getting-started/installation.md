---
title: Installation
description: Install IaC Code and verify the command.
---

# Installation

## Desktop installation

For a graphical application, install the [Desktop package](../desktop-app.md) for your operating system. It bundles the Python runtime; you do not need to install Python or run pip yourself. The Desktop guide covers installation and platform requirements.

## Web and headless installation

For local Web or command-line automation, install Python 3.10 or later, then install the package below. This route supports macOS, Linux, and Windows.

```bash
pip install iac-code
```

Verify the command:

```bash
iac-code --help
```

Run `iac-code` in an interactive terminal to start Web and open the browser. Use `iac-code web --no-open` to run the server without opening a browser. Continue with [Quick Start](./quick-start.md).

## Optional Features

Web and HTTP runtime dependencies are included in the default installation. The `[http]` extra remains a compatibility alias; a separate installation is no longer required. From a checkout, `uv sync` is sufficient.

Additional transports still use optional extras, for example `pip install 'iac-code[a2a-grpc]'` for gRPC or `pip install 'iac-code[a2a-redis]'` for Redis.

## Windows Requirements

On Windows, [Git for Windows](https://gitforwindows.org/) must be installed to provide the bash shell used by the tool execution environment.

If Git Bash is installed but not on PATH, set the `IAC_CODE_GIT_BASH_PATH` environment variable to the path of `bash.exe`:

```powershell
$env:IAC_CODE_GIT_BASH_PATH = "C:\Program Files\Git\bin\bash.exe"
```

You can install Git for Windows via winget:

```powershell
winget install --id Git.Git -e --source winget
```
