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
