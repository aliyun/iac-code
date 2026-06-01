<p align="center">
  <img src="website/static/img/logo-with-front.png" alt="iac-code" width="200">
</p>
<p align="center">
  <em>AI-powered Infrastructure as Code assistant for Alibaba Cloud (ROS / Terraform) through natural language interaction.</em>
</p>
<p align="center">
  <a href="https://github.com/aliyun/iac-code/actions/workflows/test.yml"><img src="https://github.com/aliyun/iac-code/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/v/iac-code?color=%2334D058&label=pypi%20package" alt="PyPI Package"></a>
  <a href="https://pypi.org/project/iac-code"><img src="https://img.shields.io/pypi/pyversions/iac-code?color=%2334D058&label=python" alt="Python"></a>
</p>
<p align="center">
  <strong>Language</strong>: English | <a href="readme/README.zh.md">中文</a> | <a href="readme/README.es.md">Español</a> | <a href="readme/README.fr.md">Français</a> | <a href="readme/README.de.md">Deutsch</a> | <a href="readme/README.ja.md">日本語</a> | <a href="readme/README.pt.md">Português</a>
</p>

> **Documentation**: [https://aliyun.github.io/iac-code/](https://aliyun.github.io/iac-code/)

<p align="center">
  <img src="website/static/img/demo_en.gif" alt="iac-code demo" width="100%">
</p>

## Installation

IaC Code requires Python 3.10 or later. It supports macOS, Linux, and Windows.

> **Windows note**: On Windows, [Git for Windows](https://gitforwindows.org/) must be installed to provide the bash shell used by the tool execution environment. If Git Bash is installed but not on PATH, set the `IAC_CODE_GIT_BASH_PATH` environment variable.

```bash
pip install iac-code
```

## Usage

On first use, configure the LLM provider and IaC cloud service by entering `/auth` in interactive mode.

### Interactive Mode

Run directly to enter the interactive REPL:

```bash
iac-code
```

### Non-Interactive Mode

Pass a one-shot prompt via `--prompt`:

```bash
iac-code --prompt "Create a VPC and two ECS instances"
```

Reading from stdin is also supported:

```bash
echo "Create an OSS Bucket" | iac-code --prompt -
```

## Contributing

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
make install   # install dependencies and pre-commit hooks
make dev       # run in debug mode
make test      # run tests
make lint      # run linters
make format    # format code
```

See the [Contributing Guide](https://aliyun.github.io/iac-code/getting-started/contributing) for details.

## Contact Us

| [DingTalk](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [Discord](https://discord.gg/qECFuFBwF) |
| :----------------------------------------------------------: | :----------------------------------------------------------: |
| [<img src="website/static/img/qrcode-dingtalk.jpg" width="120" height="120" alt="DingTalk">](https://qr.dingtalk.com/action/joingroup?code=v1,k1,ubm/77U7qRh/STFZUNBP26X4PNg2z6+uhiPcLGtDNfU=&_dt_no_comment=1&origin=11) | [<img src="website/static/img/qrcode-discord.jpg" width="120" height="120" alt="Discord">](https://discord.gg/qECFuFBwF) |