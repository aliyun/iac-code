---
title: Installation
description: Install IaC Code and verify the command.
---

# Installation

IaC Code requires Python 3.12 or later.

Install the package from the configured Python package index:

```bash
pip install iac-code \
  --extra-index-url http://yum.tbsite.net/aliyun-pypi/simple/ \
  --trusted-host yum.tbsite.net
```

Verify the command:

```bash
iac-code --help
```
