---
title: Contributing
description: How to set up a local environment and contribute to IaC Code.
---

# Contributing

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Setup

```bash
git clone https://github.com/aliyun/iac-code.git
cd iac-code
make install
```

`make install` installs all dependencies and sets up pre-commit hooks (lint & format checks on every commit).

## Development Workflow

Run in debug mode:

```bash
make dev
```

Run tests:

```bash
make test           # default Python version
make test PY=3.12   # specific version
make test PY=all    # all supported versions (3.10–3.14)
```

Code quality:

```bash
make lint      # ruff check + ty check
make format    # ruff format
```

Coverage:

```bash
make coverage
```

## Project Layout

```
src/iac_code/       # source code
tests/              # test suite
website/            # documentation site (Docusaurus)
```

## Submitting Changes

1. Fork the repository and create a feature branch.
2. Make your changes with tests.
3. Run `make format`, then ensure `make lint` and `make test` pass.
4. Open a pull request against `main`.
