---
title: Installation
description: Install IaC Code and verify the command.
---

# Installation

IaC Code requires Python 3.10 or later. It supports macOS, Linux, and Windows.

## Install

Install the package from the configured Python package index:

```bash
pip install iac-code
```

Verify the command:

```bash
iac-code --help
```

## Optional Features

The interactive CLI works with the base package. Some run modes depend on optional extras that you install with the `iac-code[<extra>]` syntax:

| Extra | Enables | Command |
|---|---|---|
| `http` | The local [Web App](../web-app.md) (`iac-code web`) | `pip install 'iac-code[http]'` |
| `a2a` | The [A2A server](../a2a/getting-started.md) (`iac-code a2a`) | `pip install 'iac-code[a2a]'` |

If you launch a run mode without its extra, the command fails with a message naming the extra to install. When working from a checkout of the repository, use `uv sync --extra <extra>` instead.

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
